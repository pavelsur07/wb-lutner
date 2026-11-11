"""
Тесты: lib/ym_api.py (HTTP-ретраи, проверка подключения) + страница /ui/settings/ym.

Запуск (из корня репозитория):
    venv/bin/python -m unittest tests.test_ym_api -v
"""
from __future__ import annotations

import base64
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="wb-lutner-tests-")
os.environ["DB_PATH"] = os.path.join(_TMP, "ym.sqlite")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["UI_USER"] = "u"
os.environ["UI_PASSWORD"] = "p"

from lib import config, db, ym_api  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]
config.UI_USER, config.UI_PASSWORD = "u", "p"
db.init_db()

KEY = "ACMA:secret-token:a1b2"


def resp(status: int, body: dict | None = None):
    r = mock.Mock(status_code=status, ok=200 <= status < 300, text=str(body))
    r.content = b"x"
    r.json.return_value = body or {}
    return r


CAMPAIGNS = {"campaigns": [{"id": 77, "domain": "Lutner", "business": {"id": 5, "name": "ООО"},
                            "placementType": "FBS", "apiAvailability": "AVAILABLE"}]}
WAREHOUSES = {"status": "OK", "result": {"warehouses": [
    {"id": 900, "name": "СПб", "campaignId": 77, "groupInfo": {"id": 1, "name": "Группа"}}]}}


def reset():
    conn = db.get_conn()
    conn.execute("DELETE FROM system_state WHERE key LIKE 'ym_%'")
    conn.commit()
    conn.close()
    ym_api.save_settings(api_key=KEY)


@mock.patch("lib.ym_api.time.sleep")
class Http(unittest.TestCase):
    def setUp(self):
        reset()

    def test_retry_on_420(self, _sleep):
        with mock.patch("lib.ym_api.requests.request",
                        side_effect=[resp(420), resp(200, CAMPAIGNS)]) as rq:
            self.assertEqual(ym_api.campaigns()[0]["id"], 77)
        self.assertEqual(rq.call_count, 2)
        self.assertEqual(rq.call_args.kwargs["headers"], {"Api-Key": KEY})

    def test_no_retry_on_401(self, _sleep):
        with mock.patch("lib.ym_api.requests.request", return_value=resp(401)) as rq:
            with self.assertRaises(ym_api.YMError) as e:
                ym_api.campaigns()
        self.assertEqual(rq.call_count, 1)
        self.assertEqual(e.exception.status, 401)

    def test_paging(self, _sleep):
        p1 = {"campaigns": [{"id": 1}], "paging": {"nextPageToken": "t2"}}
        p2 = {"campaigns": [{"id": 2}], "paging": {}}
        with mock.patch("lib.ym_api.requests.request", side_effect=[resp(200, p1), resp(200, p2)]) as rq:
            self.assertEqual([c["id"] for c in ym_api.campaigns()], [1, 2])
        self.assertEqual(rq.call_args.kwargs["params"], {"limit": 100, "pageToken": "t2"})

    def test_no_key(self, _sleep):
        ym_api.save_settings(api_key="")
        with self.assertRaises(ym_api.YMError):
            ym_api.campaigns()


class Check(unittest.TestCase):
    def setUp(self):
        reset()

    def test_autofill_single_store_and_warehouse(self):
        with mock.patch("lib.ym_api.requests.request",
                        side_effect=[resp(200, CAMPAIGNS), resp(200, WAREHOUSES)]) as rq:
            res = ym_api.check()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["warnings"], [])
        s = ym_api.settings()
        self.assertEqual((s["business_id"], s["campaign_id"], s["warehouse_id"]), ("5", "77", "900"))
        self.assertEqual(rq.call_args.args[1], ym_api.BASE + "/v2/businesses/5/warehouses")
        self.assertEqual(rq.call_args.kwargs["json"], {"campaignIds": [77]})
        self.assertEqual(res["warehouses"][0]["group"], "Группа")
        self.assertEqual(ym_api.last_check()["ok"], True)

    def test_403_hint_no_retry(self):
        with mock.patch("lib.ym_api.requests.request", return_value=resp(403)) as rq:
            res = ym_api.check()
        self.assertFalse(res["ok"])
        self.assertIn("нет доступа", res["error"])
        self.assertEqual(rq.call_count, 1)

    def test_wrong_saved_campaign_warns(self):
        ym_api.save_settings(business_id="5", campaign_id="13")
        with mock.patch("lib.ym_api.requests.request",
                        side_effect=[resp(200, CAMPAIGNS), resp(200, {"result": {"warehouses": []}})]):
            res = ym_api.check()
        self.assertTrue(res["ok"])
        self.assertTrue(any("13" in w for w in res["warnings"]))


class Page(unittest.TestCase):
    def setUp(self):
        reset()
        from flask import Flask
        from lib.ui import bp
        app = Flask(__name__, template_folder=os.path.join(config.BASE_DIR, "templates"))
        app.register_blueprint(bp)
        self.c = app.test_client()
        self.auth = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
        from lib.ui import _csrf_token
        self.csrf = _csrf_token()

    def test_key_masked(self):
        r = self.c.get("/ui/settings/ym", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertNotIn("secret-token", html)
        self.assertIn("••••a1b2", html)

    def test_save_requires_csrf(self):
        r = self.c.post("/ui/settings/ym", headers=self.auth, data={"business_id": "1"})
        self.assertEqual(r.status_code, 400)

    def test_empty_key_keeps_old_and_validates_ids(self):
        self.c.post("/ui/settings/ym", headers=self.auth,
                    data={"csrf": self.csrf, "api_key": "", "business_id": "5"})
        self.assertEqual(ym_api.settings()["api_key"], KEY)
        self.assertEqual(ym_api.settings()["business_id"], "5")
        r = self.c.post("/ui/settings/ym", headers=self.auth,
                        data={"csrf": self.csrf, "campaign_id": "12a"})
        self.assertIn("msg=bad_campaign_id", r.headers["Location"])
        self.assertEqual(ym_api.settings()["campaign_id"], "")

    def test_shipping_page_has_settings_nav(self):
        html = self.c.get("/ui/settings/shipping", headers=self.auth).get_data(as_text=True)
        self.assertIn("Яндекс Маркет", html)
        self.assertIn(">Настройка<", html)


if __name__ == "__main__":
    unittest.main()
