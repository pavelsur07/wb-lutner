"""
Тесты: ym_mapping.py (связка по артикулу, защита таблицы) + страница /ui/settings/ym/mapping.

Запуск (из корня репозитория):
    venv/bin/python -m unittest tests.test_ym_mapping -v
"""
from __future__ import annotations

import base64
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="wb-lutner-tests-")
os.environ["DB_PATH"] = os.path.join(_TMP, "ymmap.sqlite")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["UI_USER"] = "u"
os.environ["UI_PASSWORD"] = "p"

from lib import config, db, stock_rules, ym_api  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]
config.UI_USER, config.UI_PASSWORD = "u", "p"
db.init_db()

import ym_mapping  # noqa: E402

MIN, MAX = stock_rules.MIN_STOCK, stock_rules.MAX_STOCK


def offer(oid, campaigns=((77, "DISABLED_BY_PARTNER"),)):
    return {"offer": {"offerId": oid, "name": f"Товар {oid}", "vendor": "Cort",
                      "campaigns": [{"campaignId": c, "status": st} for c, st in campaigns]}}


def seed():
    conn = db.get_conn()
    for t in ("catalog", "stock", "ym_mapping", "system_state"):
        conn.execute(f"DELETE FROM {t}")
    # A: много (потолок), B: мало (0), C: середина (как есть), D: в каталоге без остатка
    for uuid, art, spb in (("u-a", "A", MAX + 100), ("u-b", "B", MIN - 1),
                           ("u-c", "C", MIN + 1), ("u-d", "D", None)):
        conn.execute("INSERT INTO catalog(uuid, article) VALUES (?, ?)", (uuid, art))
        if spb is not None:
            conn.execute("INSERT INTO stock(uuid, store_spb) VALUES (?, ?)", (uuid, spb))
    conn.commit()
    conn.close()
    ym_api.save_settings(api_key="k", business_id="5", campaign_id="77", warehouse_id="900")


OFFERS = [offer("A"), offer("B"), offer("C"), offer("D"),
          offer("3-22BK", campaigns=((77, "NO_STOCKS"), (1, "PUBLISHED")))]
STOCKS = [{"offerId": "A", "stocks": [{"type": "FIT", "count": 7}]}, {"offerId": "B", "stocks": []}]


def mapping():
    conn = db.get_conn()
    rows = {r["offer_id"]: dict(r) for r in conn.execute("SELECT * FROM ym_mapping")}
    conn.close()
    return rows


class Run(unittest.TestCase):
    def setUp(self):
        seed()

    def test_match_by_article_and_keep_unmatched(self):
        with mock.patch.object(ym_api, "offer_mappings", return_value=OFFERS), \
             mock.patch.object(ym_api, "stocks", return_value=STOCKS):
            self.assertEqual(ym_mapping.run(), {"total": 5, "matched": 4})
        m = mapping()
        self.assertEqual(m["A"]["lutner_uuid"], "u-a")
        self.assertEqual(m["A"]["ym_stock"], 7)
        self.assertIsNone(m["B"]["ym_stock"])            # stocks: [] -> не передавался
        self.assertIsNone(m["3-22BK"]["lutner_uuid"])    # чужой/набор — без связи
        self.assertEqual(m["3-22BK"]["ym_status"], "NO_STOCKS")  # статус именно нашего магазина

    def test_full_replace_drops_gone_offers(self):
        conn = db.get_conn()
        conn.execute("INSERT INTO ym_mapping(offer_id) VALUES ('OLD')")
        conn.commit()
        conn.close()
        with mock.patch.object(ym_api, "offer_mappings", return_value=OFFERS), \
             mock.patch.object(ym_api, "stocks", return_value=[]):
            ym_mapping.run()
        self.assertNotIn("OLD", mapping())

    def test_api_error_or_empty_keeps_table(self):
        conn = db.get_conn()
        conn.execute("INSERT INTO ym_mapping(offer_id) VALUES ('KEEP')")
        conn.commit()
        conn.close()
        with mock.patch.object(ym_api, "offer_mappings", return_value=[]):
            self.assertRaises(ym_api.YMError, ym_mapping.run)
        with mock.patch.object(ym_api, "offer_mappings", return_value=OFFERS), \
             mock.patch.object(ym_api, "stocks", side_effect=ym_api.YMError("403", 403)):
            self.assertRaises(ym_api.YMError, ym_mapping.run)
        self.assertEqual(list(mapping()), ["KEEP"])


class Push(unittest.TestCase):
    def setUp(self):
        seed()
        with mock.patch.object(ym_api, "offer_mappings", return_value=OFFERS), \
             mock.patch.object(ym_api, "stocks", return_value=[]):
            ym_mapping.run()

    def test_only_matched_with_rules(self):
        import sync_stock_to_ym
        with mock.patch.object(ym_api, "update_stocks", return_value={"status": "OK"}) as up:
            self.assertEqual(sync_stock_to_ym.run(), 0)
        biz, items = up.call_args.args
        self.assertEqual(biz, "5")
        got = {i["sku"]: i["count"] for i in items}
        self.assertEqual(got, {"A": MAX, "B": 0, "C": MIN + 1, "D": 0})  # 3-22BK не шлём
        self.assertTrue(all(i["partnerWarehouseId"] == 900 for i in items))
        self.assertEqual(db.get_state("last_stock_to_ym"), "ok")

    def test_dry_run_and_only(self):
        import sync_stock_to_ym
        with mock.patch.object(ym_api, "update_stocks") as up:
            self.assertEqual(sync_stock_to_ym.run(dry_run=True), 0)
            up.assert_not_called()
            sync_stock_to_ym.run(only="C")
        self.assertEqual(up.call_args.args[1], [{"sku": "C", "partnerWarehouseId": 900,
                                                 "count": MIN + 1}])
        self.assertIsNone(db.get_state("last_stock_to_ym"))  # тестовый --only не отметка цикла

    def test_failure_alerts(self):
        import sync_stock_to_ym
        with mock.patch.object(ym_api, "update_stocks", side_effect=ym_api.YMError("403", 403)), \
             mock.patch.object(sync_stock_to_ym, "alert") as al:
            self.assertEqual(sync_stock_to_ym.run(), 1)
        al.assert_called_once()


class Page(unittest.TestCase):
    def setUp(self):
        seed()
        with mock.patch.object(ym_api, "offer_mappings", return_value=OFFERS), \
             mock.patch.object(ym_api, "stocks", return_value=STOCKS):
            ym_mapping.run()
        from flask import Flask
        from lib.ui import _csrf_token, bp
        app = Flask(__name__, template_folder=os.path.join(config.BASE_DIR, "templates"))
        app.register_blueprint(bp)
        self.c = app.test_client()
        self.auth = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
        self.csrf = _csrf_token()

    def test_rules_shown(self):
        html = self.c.get("/ui/settings/ym/mapping", headers=self.auth).get_data(as_text=True)
        self.assertIn(f"потолок {MAX}", html)            # A
        self.assertIn(f"меньше {MIN} → 0", html)         # B
        self.assertIn("как есть", html)                  # C
        self.assertIn("нет остатка в Lutner → 0", html)  # D
        self.assertNotIn("3-22BK", html)                 # по умолчанию только сопоставленные
        self.assertIn("3-22BK", self.c.get("/ui/settings/ym/mapping?f=unmatched",
                                           headers=self.auth).get_data(as_text=True))

    def test_refresh_requires_csrf_and_shows_error(self):
        self.assertEqual(self.c.post("/ui/settings/ym/mapping/refresh",
                                     headers=self.auth).status_code, 400)
        with mock.patch.object(ym_api, "offer_mappings",
                               side_effect=ym_api.YMError("нет доступа", 403)):
            r = self.c.post("/ui/settings/ym/mapping/refresh", headers=self.auth,
                            data={"csrf": self.csrf}, follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn("нет доступа", html)
        self.assertIn("A", mapping())                    # таблица не тронута

    def test_ym_orders_page(self):
        r = self.c.get("/ui/ym", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Заказов пока нет", r.get_data(as_text=True))

        conn = db.get_conn()
        conn.execute("INSERT INTO ym_orders(ym_order_id, lutner_order_id, status, items_json) "
                     "VALUES (123456789, 'L-1', 'created', '[]')")
        conn.commit()
        conn.close()
        html = self.c.get("/ui/ym", headers=self.auth).get_data(as_text=True)
        self.assertIn("123456789", html)
        self.assertIn("L-1", html)
        # активна вкладка «Яндекс Маркет», а не «Настройка»
        self.assertIn('nav-link active" href="/ui/ym"', html)
        self.assertNotIn('nav-link active" href="/ui/settings/shipping"', html)


if __name__ == "__main__":
    unittest.main()
