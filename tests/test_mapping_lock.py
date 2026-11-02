"""
Тесты: initial_mapping / ozon_mapping НЕ держат блокировку SQLite на запись,
пока ходят в API маркетплейса.

Причина задачи: прод 02.11.2026 — initial_mapping держал write-lock ~5 мин,
пока листал WB cards_list; webhook и sync_stock_* падали с
`sqlite3.OperationalError: database is locked`.

Запуск (из корня репозитория, без внешних зависимостей):
    venv/bin/python -m unittest tests.test_mapping_lock -v
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

# Ядро конфига валидируется при импорте — подставляем временные пути ДО импорта lib.
_TMP = tempfile.mkdtemp(prefix="wb-lutner-tests-")
os.environ["DB_PATH"] = os.path.join(_TMP, "boot.sqlite")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")

from lib import config, db, ozon_api, wb_api  # noqa: E402
import initial_mapping  # noqa: E402
import migrate_ozon  # noqa: E402
import ozon_mapping  # noqa: E402

WB_PAGE = 100     # initial_mapping запрашивает следующую страницу, если total >= 100
OZON_PAGE = 1000  # ozon_api.iter_products идёт дальше, если items == 1000


def _db_write_is_free(path: str) -> bool:
    """Пробует взять write-lock из ДРУГОГО соединения без ожидания."""
    probe = sqlite3.connect(path, timeout=0)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()
        return True
    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            return False
        raise
    finally:
        probe.close()


def _wb_card(i: int, article: str | None = None) -> dict:
    return {
        "nmID": 1000 + i,
        "vendorCode": article if article is not None else f"ART-{i}",
        "sizes": [{"skus": [f"BC-{i}"]}],
    }


def _ozon_item(i: int, article: str | None = None) -> dict:
    return {
        "offer_id": article if article is not None else f"ART-{i}",
        "product_id": 5000 + i,
        "sku": 9000 + i,
    }


class _DbCase(unittest.TestCase):
    """Своя БД на каждый тест + каталог Lutner с артикулами ART-0..ART-(n-1)."""

    CATALOG_SIZE = 150

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite", dir=_TMP)
        os.close(fd)
        os.unlink(self.db_path)
        p = mock.patch.object(config, "DB_PATH", self.db_path)
        p.start()
        self.addCleanup(p.stop)

        db.init_db()
        with mock.patch("builtins.print"):
            migrate_ozon.main()

        conn = db.get_conn()
        conn.executemany(
            "INSERT INTO catalog (uuid, article) VALUES (?, ?)",
            [(f"uuid-{i}", f"ART-{i}") for i in range(self.CATALOG_SIZE)],
        )
        conn.commit()
        conn.close()

    def _rows(self, sql: str) -> list[tuple]:
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute(sql)]
        finally:
            conn.close()

    def _run(self, module):
        with mock.patch("builtins.print"):
            return module.run()


class WbMappingTest(_DbCase):

    def _fake_cards_list(self, pages: list, on_call=None):
        """pages[i] — список карточек i-й страницы, или Exception для сбоя."""
        calls = {"n": 0}

        def fake(cursor=None, limit=100):
            n = calls["n"]
            calls["n"] += 1
            if on_call:
                on_call(n)
            page = pages[n]
            if isinstance(page, Exception):
                raise page
            last = page[-1] if page else {}
            return {
                "cards": page,
                "cursor": {"total": len(page), "updatedAt": "x",
                           "nmID": last.get("nmID")},
            }
        return fake

    def test_no_write_lock_while_fetching_next_page(self):
        """RED до фикса: на запросе 2-й страницы БД должна быть свободна на запись."""
        page1 = [_wb_card(i) for i in range(WB_PAGE)]
        page2 = [_wb_card(i) for i in range(WB_PAGE, WB_PAGE + 10)]
        free_on_call: dict[int, bool] = {}

        def probe(n):
            free_on_call[n] = _db_write_is_free(self.db_path)

        with mock.patch.object(wb_api, "cards_list",
                               self._fake_cards_list([page1, page2], probe)):
            self.assertEqual(self._run(initial_mapping), 0)

        self.assertEqual(set(free_on_call), {0, 1})
        self.assertTrue(free_on_call[1],
                        "initial_mapping держит write-lock, пока ходит в WB API")

    def test_mapping_content(self):
        """Регрессия: связки пишутся по артикулу, мусорные карточки пропускаются."""
        page = [
            _wb_card(1),
            _wb_card(2),
            _wb_card(3, article="NO-SUCH-ART"),                       # нет в catalog
            {"nmID": 4, "vendorCode": "ART-4", "sizes": []},          # нет баркода
            {"nmID": 5, "vendorCode": "  ", "sizes": [{"skus": ["BC-5"]}]},  # нет артикула
        ]
        with mock.patch.object(wb_api, "cards_list", self._fake_cards_list([page])):
            self.assertEqual(self._run(initial_mapping), 0)

        self.assertEqual(
            self._rows("SELECT wb_barcode, wb_nmid, lutner_uuid, lutner_article "
                       "FROM mapping ORDER BY wb_barcode"),
            [("BC-1", 1001, "uuid-1", "ART-1"),
             ("BC-2", 1002, "uuid-2", "ART-2")],
        )

    def test_upsert_updates_existing(self):
        """Регрессия: повторный прогон обновляет существующую связку, не дублирует."""
        conn = db.get_conn()
        conn.execute("INSERT INTO mapping VALUES ('BC-1', 1, 'old-uuid', 'OLD')")
        conn.commit()
        conn.close()

        with mock.patch.object(wb_api, "cards_list",
                               self._fake_cards_list([[_wb_card(1)]])):
            self._run(initial_mapping)

        self.assertEqual(self._rows("SELECT * FROM mapping"),
                         [("BC-1", 1001, "uuid-1", "ART-1")])

    def test_api_failure_midway_keeps_mapping_untouched(self):
        """Атомарность: сбой WB на 2-й странице — mapping не меняется частично."""
        conn = db.get_conn()
        conn.execute("INSERT INTO mapping VALUES ('BC-0', 1, 'old-uuid', 'OLD')")
        conn.commit()
        conn.close()

        page1 = [_wb_card(i) for i in range(WB_PAGE)]
        boom = RuntimeError("WB POST /content/v2/get/cards/list failed after retries: 500")
        with mock.patch.object(wb_api, "cards_list",
                               self._fake_cards_list([page1, boom])):
            with self.assertRaises(RuntimeError):
                self._run(initial_mapping)

        self.assertEqual(self._rows("SELECT * FROM mapping"),
                         [("BC-0", 1, "old-uuid", "OLD")])


class OzonMappingTest(_DbCase):

    CATALOG_SIZE = OZON_PAGE + 20

    def _fake_product_list(self, pages: list, on_call=None):
        calls = {"n": 0}

        def fake(last_id=""):
            n = calls["n"]
            calls["n"] += 1
            if on_call:
                on_call(n)
            page = pages[n]
            if isinstance(page, Exception):
                raise page
            return {"items": page, "last_id": f"cursor-{n}" if page else ""}
        return fake

    def test_no_write_lock_while_fetching_next_page(self):
        """RED до фикса: на запросе 2-й страницы БД должна быть свободна на запись."""
        page1 = [_ozon_item(i) for i in range(OZON_PAGE)]
        page2 = [_ozon_item(i) for i in range(OZON_PAGE, OZON_PAGE + 5)]
        free_on_call: dict[int, bool] = {}

        def probe(n):
            free_on_call[n] = _db_write_is_free(self.db_path)

        with mock.patch.object(ozon_api, "product_list",
                               self._fake_product_list([page1, page2], probe)):
            self.assertEqual(self._run(ozon_mapping), 0)

        self.assertEqual(set(free_on_call), {0, 1})
        self.assertTrue(free_on_call[1],
                        "ozon_mapping держит write-lock, пока ходит в Ozon API")

    def test_mapping_content(self):
        page = [_ozon_item(1), _ozon_item(2, article="NO-SUCH-ART"),
                {"offer_id": " ", "product_id": 1, "sku": 1}]
        with mock.patch.object(ozon_api, "product_list",
                               self._fake_product_list([page])):
            self.assertEqual(self._run(ozon_mapping), 0)

        self.assertEqual(self._rows("SELECT * FROM ozon_mapping"),
                         [("ART-1", 5001, 9001, "uuid-1", "ART-1")])

    def test_api_failure_midway_keeps_mapping_untouched(self):
        conn = db.get_conn()
        conn.execute("INSERT INTO ozon_mapping VALUES ('ART-0', 1, 1, 'old-uuid', 'OLD')")
        conn.commit()
        conn.close()

        page1 = [_ozon_item(i) for i in range(OZON_PAGE)]
        boom = RuntimeError("Ozon /v3/product/list failed after retries: 429")
        with mock.patch.object(ozon_api, "product_list",
                               self._fake_product_list([page1, boom])):
            with self.assertRaises(RuntimeError):
                self._run(ozon_mapping)

        self.assertEqual(self._rows("SELECT * FROM ozon_mapping"),
                         [("ART-0", 1, 1, "old-uuid", "OLD")])


if __name__ == "__main__":
    unittest.main()
