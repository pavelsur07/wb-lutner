"""
Тесты: ym_main.py — заказы Яндекс Маркета -> заказы Lutner.

Запуск (из корня репозитория):
    venv/bin/python -m unittest tests.test_ym_main -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="wb-lutner-tests-")
os.environ["DB_PATH"] = os.path.join(_TMP, "ymmain.sqlite")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")

from lib import config, db, ym_api  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]
db.init_db()

import ym_main  # noqa: E402

CAMP = "77"


def iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def order(oid, *, items=(("A", 1),), sub="STARTED", camp=77, fake=False, age=1,
          ym_ship=None):
    return {"orderId": oid, "campaignId": camp, "status": "PROCESSING", "substatus": sub,
            "fake": fake, "creationDate": iso(age),
            "items": [{"offerId": o, "count": c} for o, c in items],
            "delivery": {"shipment": {"shipmentDate": ym_ship}} if ym_ship else {}}


def seed():
    conn = db.get_conn()
    for t in ("catalog", "stock", "ym_mapping", "ym_orders", "system_state"):
        conn.execute(f"DELETE FROM {t}")
    for uuid, art, spb in (("u-a", "A", 10), ("u-b", "B", 0)):
        conn.execute("INSERT INTO catalog(uuid, article) VALUES (?, ?)", (uuid, art))
        conn.execute("INSERT INTO stock(uuid, store_spb, store_ekb) VALUES (?, ?, 3)", (uuid, spb))
        conn.execute("INSERT INTO ym_mapping(offer_id, lutner_uuid, lutner_article) "
                     "VALUES (?, ?, ?)", (art, uuid, art))
    conn.execute("INSERT INTO ym_mapping(offer_id) VALUES ('FOREIGN')")  # чужой товар
    conn.commit()
    conn.close()
    ym_api.save_settings(api_key="k", business_id="5", campaign_id=CAMP)


def saved():
    conn = db.get_conn()
    rows = {r["ym_order_id"]: dict(r) for r in conn.execute("SELECT * FROM ym_orders")}
    conn.close()
    return rows


class Run(unittest.TestCase):
    def setUp(self):
        seed()
        self.alert = mock.patch.object(ym_main, "alert").start()
        self.create = mock.patch.object(ym_main.lutner_api, "create_order",
                                        return_value={"order_id": 555}).start()
        self.addCleanup(mock.patch.stopall)

    def go(self, orders, dry_run=False, fake=False):
        with mock.patch.object(ym_api, "orders", return_value=orders) as o:
            rc = ym_main.run(dry_run, fake)
        o.assert_called_once_with("5", CAMP, fake=fake)
        return rc

    def test_creates_lutner_order(self):
        self.go([order(1, items=(("A", 2), ("A", 1)))])
        payload = self.create.call_args.args[0]
        self.assertEqual(payload["items"], {"A": 3})
        self.assertTrue(payload["dropshipping"])
        self.assertRegex(payload["comment"],
                         r"^Яндекс Маркет, отгрузка \d\d\.\d\d\.\d{4}, Заказ 1$")
        row = saved()[1]
        self.assertEqual((row["status"], row["lutner_order_id"]), ("created", "555"))
        self.assertIsNotNone(db.get_state("last_cycle_ym"))

    def test_idempotent(self):
        self.go([order(1)])
        self.go([order(1)])
        self.assertEqual(self.create.call_count, 1)

    def test_skips_fake_foreign_status_stale(self):
        self.go([order(2, fake=True), order(3, camp=999), order(4, sub="SHIPPED"),
                 order(5, age=48)])
        self.create.assert_not_called()
        rows = saved()
        self.assertNotIn(3, rows)                         # чужой магазин не пишем вовсе
        self.assertEqual(rows[2]["status"], "skipped_status")
        self.assertIn("fake", rows[2]["comment"])
        self.assertEqual(rows[4]["status"], "skipped_status")
        self.assertEqual(rows[5]["status"], "skipped_stale")
        self.alert.assert_called_once()                   # про старый заказ

    def test_no_mapping_or_no_stock_fails_whole_order(self):
        self.go([order(6, items=(("A", 1), ("FOREIGN", 1))), order(7, items=(("B", 1),))])
        self.create.assert_not_called()
        rows = saved()
        self.assertEqual(rows[6]["status"], "failed")
        self.assertEqual(json.loads(rows[6]["error_json"])[0]["reason"], "no_mapping")
        self.assertEqual(json.loads(rows[7]["error_json"])[0]["reason"], "no_stock_spb")
        self.assertEqual(self.alert.call_count, 2)

    def test_fake_mode_processes_test_order_marked(self):
        self.go([order(13, fake=True)], fake=True)
        comment = self.create.call_args.args[0]["comment"]
        self.assertTrue(comment.startswith("ТЕСТ, не отгружать! Яндекс Маркет, отгрузка "))
        self.assertEqual(saved()[13]["status"], "created")
        self.assertIsNone(db.get_state("last_cycle_ym"))  # тест не отметка рабочего цикла

    def test_fake_mode_dry_run(self):
        self.go([order(14, fake=True)], dry_run=True, fake=True)
        self.create.assert_not_called()
        self.assertEqual(saved(), {})

    def test_lutner_rejects(self):
        self.create.return_value = {"errors": ["bad"]}
        self.go([order(8)])
        self.assertEqual(saved()[8]["status"], "failed")

    def test_dry_run_writes_nothing(self):
        self.go([order(9), order(10, fake=True), order(11, age=48)], dry_run=True)
        self.create.assert_not_called()
        self.alert.assert_not_called()
        self.assertEqual(saved(), {})
        self.assertIsNone(db.get_state("last_cycle_ym"))

    def test_fetch_error_alerts(self):
        with mock.patch.object(ym_api, "orders", side_effect=ym_api.YMError("420", 420)):
            self.assertEqual(ym_main.run(False), 1)
        self.alert.assert_called_once()

    def test_ym_earlier_shipment_warns(self):
        with self.assertLogs(ym_main.log, "WARNING"):
            self.go([order(12, ym_ship="2000-01-01")])
        self.assertEqual(saved()[12]["status"], "created")


if __name__ == "__main__":
    unittest.main()
