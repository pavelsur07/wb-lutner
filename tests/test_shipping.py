"""
Тесты: дата отгрузки (lib/shipping.py) с отдельным графиком выходных для
WB и Ozon + страница /ui/settings/shipping.

Запуск (из корня репозитория):
    venv/bin/python -m unittest tests.test_shipping -v
"""
from __future__ import annotations

import base64
import os
import re
import tempfile
import unittest
from datetime import date, datetime

_TMP = tempfile.mkdtemp(prefix="wb-lutner-tests-")
os.environ["DB_PATH"] = os.path.join(_TMP, "shipping.sqlite")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["UI_USER"] = "u"
os.environ["UI_PASSWORD"] = "p"

from lib import config, db, shipping  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]
config.UI_USER, config.UI_PASSWORD = "u", "p"
db.init_db()

import main  # noqa: E402
import ozon_main  # noqa: E402

OFF = {"sat": False, "sun": False}


def msk(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=shipping.MSK)


class ShipDateRule(unittest.TestCase):
    # 06.11.2026 — пятница, 07.11 — суббота, 08.11 — воскресенье, 09.11 — Пн

    def test_cutoff_weekday(self):
        self.assertEqual(shipping.ship_date(msk(2026, 11, 3, 7, 0), OFF), date(2026, 11, 3))
        self.assertEqual(shipping.ship_date(msk(2026, 11, 3, 7, 1), OFF), date(2026, 11, 4))

    def test_weekend_off_goes_to_monday(self):
        self.assertEqual(shipping.ship_date(msk(2026, 11, 6, 7, 1), OFF), date(2026, 11, 9))
        self.assertEqual(shipping.ship_date(msk(2026, 11, 7, 3, 0), OFF), date(2026, 11, 9))
        self.assertEqual(shipping.ship_date(msk(2026, 11, 8, 23, 0), OFF), date(2026, 11, 9))

    def test_saturday_working(self):
        wk = {"sat": True, "sun": False}
        self.assertEqual(shipping.ship_date(msk(2026, 11, 6, 10, 0), wk), date(2026, 11, 7))
        # граница 07:00 действует и в субботу
        self.assertEqual(shipping.ship_date(msk(2026, 11, 7, 7, 0), wk), date(2026, 11, 7))
        self.assertEqual(shipping.ship_date(msk(2026, 11, 7, 7, 1), wk), date(2026, 11, 9))

    def test_sunday_only_working(self):
        wk = {"sat": False, "sun": True}
        self.assertEqual(shipping.ship_date(msk(2026, 11, 6, 10, 0), wk), date(2026, 11, 8))
        self.assertEqual(shipping.ship_date(msk(2026, 11, 8, 7, 1), wk), date(2026, 11, 9))

    def test_all_week_working(self):
        wk = {"sat": True, "sun": True}
        self.assertEqual(shipping.ship_date(msk(2026, 11, 7, 10, 0), wk), date(2026, 11, 8))

    def test_utc_input(self):
        # 04:30 UTC = 07:30 МСК -> следующий день
        self.assertEqual(shipping.shipment_date_str("wb", "2026-11-03T04:30:00Z"), "04.11.2026")


class PerMarketSettings(unittest.TestCase):
    def setUp(self):
        conn = db.get_conn()
        conn.execute("DELETE FROM system_state")
        conn.commit()
        conn.close()

    def test_default_is_weekend_off(self):
        self.assertEqual(shipping.get_weekend("wb"), OFF)
        self.assertEqual(shipping.get_weekend("ozon"), OFF)

    def test_markets_are_independent(self):
        shipping.set_weekend("ozon", sat=True, sun=False)
        fri_10_utc = "2026-11-06T07:00:00Z"  # 10:00 МСК, пятница
        self.assertEqual(ozon_main._shipment_date({"in_process_at": fri_10_utc}), "07.11.2026")
        self.assertEqual(main._shipment_date({"createdAt": fri_10_utc}), "09.11.2026")

    def test_garbage_in_state_falls_back(self):
        db.set_state("ship_weekend_wb", "not json")
        self.assertEqual(shipping.get_weekend("wb"), OFF)


class SettingsPage(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        from lib.ui import bp
        app = Flask(__name__, template_folder=os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates"))
        app.register_blueprint(bp)
        self.client = app.test_client()
        tok = base64.b64encode(b"u:p").decode()
        self.auth = {"Authorization": f"Basic {tok}"}
        conn = db.get_conn()
        conn.execute("DELETE FROM system_state")
        conn.commit()
        conn.close()

    def _csrf(self):
        html = self.client.get("/ui/settings/shipping", headers=self.auth).get_data(as_text=True)
        return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)

    def test_requires_auth(self):
        self.assertEqual(self.client.get("/ui/settings/shipping").status_code, 401)

    def test_page_renders(self):
        r = self.client.get("/ui/settings/shipping", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Wildberries", html)
        self.assertIn("Ozon", html)

    def test_save_only_target_market(self):
        r = self.client.post("/ui/settings/shipping/ozon", headers=self.auth,
                             data={"csrf": self._csrf(), "sun": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(shipping.get_weekend("ozon"), {"sat": False, "sun": True})
        self.assertEqual(shipping.get_weekend("wb"), OFF)

    def test_save_without_csrf_rejected(self):
        r = self.client.post("/ui/settings/shipping/wb", headers=self.auth, data={"sat": "1"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(shipping.get_weekend("wb"), OFF)

    def test_unknown_market_404(self):
        r = self.client.post("/ui/settings/shipping/yandex", headers=self.auth,
                             data={"csrf": self._csrf()})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
