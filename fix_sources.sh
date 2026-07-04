#!/usr/bin/env bash
# Приводит исходники wb-lutner к каноническому состоянию. Идемпотентно.
# НЕ трогает: .env, data/, logs/, venv/, .git/ и твои вспомогательные файлы —
# переписывает ТОЛЬКО перечисленные .py по правильным путям.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
echo "Рабочая папка: $(pwd)"
mkdir -p lib data logs
touch lib/__init__.py
# два файла ошибочно лежат в КОРНЕ — их место в lib/. Убираем дубли из корня:
rm -f ./logging_setup.py ./lutner_api.py
cat > lib/config.py <<'WBLUTNER_CANON_EOF'
"""
lib/config.py — загрузка и валидация конфигурации из .env.

Принцип: падаем при СТАРТЕ, а не в 3 часа ночи.
- Ядро (DB_PATH, LOG_DIR) валидируется при импорте модуля.
- Фичеспецифичные ключи (токены WB/Lutner, SMTP) проверяет каждый
  скрипт сам через config.require("WB_API_TOKEN", ...), чтобы на раннем
  этапе можно было тестировать БД без заполненных токенов.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class ConfigError(RuntimeError):
    pass


def _get(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is not None:
        val = val.strip()
    return val or default


# --- Ядро (нужно всем) ---
DB_PATH = _get("DB_PATH", str(BASE_DIR / "data" / "db.sqlite"))
LOG_DIR = _get("LOG_DIR", str(BASE_DIR / "logs"))
TZ = _get("TZ", "Europe/Moscow")

# --- Lutner ---
LUTNER_API_BASE = _get("LUTNER_API_BASE", "https://lutner.ru/local/api/")
LUTNER_API_TOKEN = _get("LUTNER_API_TOKEN")
LUTNER_PROFILE_ID = _get("LUTNER_PROFILE_ID")
LUTNER_CATALOG_FULL_URL = _get("LUTNER_CATALOG_FULL_URL")
LUTNER_CATALOG_SHORT_URL = _get("LUTNER_CATALOG_SHORT_URL")
CSV_DELIMITER = _get("CSV_DELIMITER", ";")

# --- Wildberries ---
WB_API_BASE = _get("WB_API_BASE", "https://marketplace-api.wildberries.ru")
WB_CONTENT_API_BASE = _get("WB_CONTENT_API_BASE", "https://content-api.wildberries.ru")
WB_API_TOKEN = _get("WB_API_TOKEN")
WB_WAREHOUSE_ID = _get("WB_WAREHOUSE_ID")

# --- Webhook ---
WEBHOOK_SECRET_PATH = _get("WEBHOOK_SECRET_PATH")

# --- SMTP (Яндекс.Почта, пароль приложения) ---
SMTP_HOST = _get("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT = int(_get("SMTP_PORT", "465"))
SMTP_USER = _get("SMTP_USER")
SMTP_PASSWORD = _get("SMTP_PASSWORD")
ALERT_TO = _get("ALERT_TO")
MANAGER_TO = _get("MANAGER_TO")

# --- Бизнес ---
LEGAL_ENTITY = _get("LEGAL_ENTITY", "ООО Ромашка")
MARKETPLACE_NAME = _get("MARKETPLACE_NAME", "Wildberries")

_CORE_REQUIRED = ["DB_PATH", "LOG_DIR"]


def require(*names: str) -> None:
    """Проверяет, что перечисленные ключи заданы и непусты. Иначе ConfigError."""
    missing = [n for n in names if not globals().get(n)]
    if missing:
        raise ConfigError(
            ".env: не заданы обязательные переменные: " + ", ".join(missing)
        )


# Валидируем ядро сразу при импорте — упасть при старте, а не ночью.
require(*_CORE_REQUIRED)
WBLUTNER_CANON_EOF
cat > lib/db.py <<'WBLUTNER_CANON_EOF'
"""
lib/db.py — SQLite (WAL) + идемпотентные миграции.

Создание/миграция БД:
    venv/bin/python -m lib.db
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from lib import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog (
    uuid            TEXT PRIMARY KEY,
    article         TEXT,
    barcode         TEXT,
    name            TEXT,
    brand           TEXT,
    package_length  REAL,
    package_width   REAL,
    package_height  REAL,
    weight          REAL,
    active          INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_catalog_article ON catalog(article);
CREATE INDEX IF NOT EXISTS idx_catalog_barcode ON catalog(barcode);

CREATE TABLE IF NOT EXISTS stock (
    uuid          TEXT PRIMARY KEY,
    quantity      INTEGER NOT NULL DEFAULT 0,
    store_spb     INTEGER NOT NULL DEFAULT 0,
    store_ekb     INTEGER NOT NULL DEFAULT 0,
    price_retail  REAL,
    price_diller  REAL,
    price_mp      REAL,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mapping (
    wb_barcode     TEXT PRIMARY KEY,
    wb_nmid        INTEGER,
    lutner_uuid    TEXT,
    lutner_article TEXT
);
CREATE INDEX IF NOT EXISTS idx_mapping_uuid ON mapping(lutner_uuid);

CREATE TABLE IF NOT EXISTS orders (
    wb_order_id        INTEGER PRIMARY KEY,
    lutner_order_id    TEXT,
    status             TEXT NOT NULL,
    comment            TEXT,
    items_json         TEXT,
    error_json         TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    sent_to_manager_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status  ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);

CREATE TABLE IF NOT EXISTS webhook_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_ip     TEXT,
    forwarded_for TEXT,
    user_agent    TEXT,
    body_size     INTEGER,
    items_count   INTEGER,
    status        TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_webhook_created ON webhook_log(created_at);

CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def set_state(key: str, value: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO system_state(key, value, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, updated_at=datetime('now')",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def get_state(key: str, default: str | None = None) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    conn = get_conn()
    tables = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    print(f"OK  db: {config.DB_PATH}")
    print(f"    journal_mode = {mode}")
    print(f"    tables: {', '.join(tables)}")
WBLUTNER_CANON_EOF
cat > lib/logging_setup.py <<'WBLUTNER_CANON_EOF'
"""lib/logging_setup.py — единый логгер: файл с ротацией (10MB×5) + stderr."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from lib import config

_FMT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    os.makedirs(config.LOG_DIR, exist_ok=True)

    fh = RotatingFileHandler(
        os.path.join(config.LOG_DIR, f"{name}.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(sh)
    return logger
WBLUTNER_CANON_EOF
cat > lib/mailer.py <<'WBLUTNER_CANON_EOF'
"""
lib/mailer.py — отправка писем и алертов через SMTP_SSL (Яндекс).

Правило: ошибки SMTP логируются, но НЕ пробрасываются — мы не хотим
падать из-за упавшего почтового сервера.
"""
from __future__ import annotations

import mimetypes
import smtplib
from email.message import EmailMessage

from lib import config
from lib.logging_setup import get_logger

log = get_logger("mailer")


def send_email(to: str, subject: str, body: str, attachments=None) -> bool:
    """
    attachments: list[tuple[str filename, bytes data]].
    Возвращает True при успехе, False при ошибке (исключений не бросает).
    """
    try:
        config.require("SMTP_USER", "SMTP_PASSWORD")
    except config.ConfigError as e:
        log.error("mail not configured: %s", e)
        return False

    msg = EmailMessage()
    msg["From"] = config.SMTP_USER
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    for fname, data in attachments or []:
        ctype, _ = mimetypes.guess_type(fname)
        maintype, subtype = (
            ctype.split("/", 1) if ctype else ("application", "octet-stream")
        )
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)

    try:
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as srv:
            srv.login(config.SMTP_USER, config.SMTP_PASSWORD)
            srv.send_message(msg)
        log.info("sent '%s' -> %s", subject, to)
        return True
    except Exception as e:  # noqa: BLE001 — намеренно глушим любую ошибку SMTP
        log.error("SMTP send failed (%s): %s", subject, e)
        return False


def alert(subject: str, body: str) -> bool:
    """Быстрый алерт на ALERT_TO (или SMTP_USER, если ALERT_TO не задан)."""
    to = config.ALERT_TO or config.SMTP_USER or ""
    return send_email(to, f"[wb-lutner] {subject}", body)
WBLUTNER_CANON_EOF
cat > lib/lutner_api.py <<'WBLUTNER_CANON_EOF'
"""
lib/lutner_api.py — обёртки над Lutner Rest API.

- RateLimiter: скользящее окно, thread-safe (webhook-сервер многопоточный).
- Лимиты Lutner: 10 req/min, 500 req/day. Держим запас: 8/60с + дневной счётчик.
- Retry с экспонентой 2/4/8с на сетевых/5xx; НЕ ретраим 400/401/403.
- Таймаут 30с.

Endpoint'ы: GET /profile/, POST /order/, POST /ordercancel/
Авторизация: заголовок  Api-token: <token>
"""
from __future__ import annotations

import threading
import time
from collections import deque
from urllib.parse import urljoin

import requests

from lib import config, db
from lib.logging_setup import get_logger

log = get_logger("lutner_api")

TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF = [2, 4, 8]
NO_RETRY_CODES = {400, 401, 403}
DAILY_CAP = 480  # запас от жёстких 500/сутки


class RateLimiter:
    """Скользящее окно: не более max_calls за period секунд. Thread-safe."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                sleep_for = self.period - (now - self._calls[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)


_limiter = RateLimiter(max_calls=8, period=60)


def _check_daily_cap() -> None:
    from datetime import date

    today = date.today().isoformat()
    key = "lutner_calls"
    raw = db.get_state(key, "")
    stored_day, _, cnt = (raw.partition(":") + ("",))[:3] if raw else ("", "", "")
    cnt = int(cnt) if raw and cnt.isdigit() else 0
    if stored_day != today:
        cnt = 0
    if cnt >= DAILY_CAP:
        raise RuntimeError(f"Lutner daily cap reached ({cnt}/{DAILY_CAP})")
    db.set_state(key, f"{today}:{cnt + 1}")


def _headers() -> dict:
    config.require("LUTNER_API_TOKEN")
    return {"Api-token": config.LUTNER_API_TOKEN, "Content-Type": "application/json"}


def _request(method: str, path: str, json_body=None) -> dict:
    url = urljoin(config.LUTNER_API_BASE, path.lstrip("/"))
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        _limiter.acquire()
        _check_daily_cap()
        try:
            resp = requests.request(
                method, url, headers=_headers(), json=json_body, timeout=TIMEOUT
            )
            if resp.status_code in NO_RETRY_CODES:
                log.warning("Lutner %s %s -> %s (no retry)", method, path, resp.status_code)
                resp.raise_for_status()
            if resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} server error")
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", 0) in NO_RETRY_CODES:
                raise
            last_exc = e
        except requests.RequestException as e:
            last_exc = e
        if attempt < MAX_RETRIES:
            wait = BACKOFF[attempt]
            log.warning("Lutner %s %s failed (%s), retry in %ss", method, path, last_exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Lutner {method} {path} failed after retries: {last_exc}")


def get_profiles() -> dict:
    return _request("GET", "profile/")


def create_order(payload: dict) -> dict:
    """
    payload например:
    {profile, warehouse:'spb', dropshipping:True, dropshipping_count:1,
     items:{article: qty}, comment:'...'}
    """
    return _request("POST", "order/", json_body=payload)


def cancel_order(payload: dict) -> dict:
    return _request("POST", "ordercancel/", json_body=payload)
WBLUTNER_CANON_EOF
cat > lib/wb_api.py <<'WBLUTNER_CANON_EOF'
"""
lib/wb_api.py — обёртки над Wildberries API.

Marketplace API: https://marketplace-api.wildberries.ru
Content API:     https://content-api.wildberries.ru
Авторизация: заголовок Authorization: <JWT-токен>

Retry/таймауты — аналогично lutner_api. WB отдаёт 429 при превышении лимитов —
на него ретраим с бэкоффом.
"""
from __future__ import annotations

import time

import requests

from lib import config
from lib.lutner_api import RateLimiter
from lib.logging_setup import get_logger

log = get_logger("wb_api")

TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF = [2, 4, 8]
NO_RETRY_CODES = {400, 401, 403, 404}

_limiter = RateLimiter(max_calls=30, period=60)  # консервативно


def _headers() -> dict:
    config.require("WB_API_TOKEN")
    return {"Authorization": config.WB_API_TOKEN, "Content-Type": "application/json"}


def _request(method: str, base: str, path: str, *, params=None, json_body=None):
    url = base.rstrip("/") + path
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        _limiter.acquire()
        try:
            resp = requests.request(
                method, url, headers=_headers(), params=params,
                json=json_body, timeout=TIMEOUT,
            )
            if resp.status_code in NO_RETRY_CODES:
                log.warning("WB %s %s -> %s (no retry): %s",
                            method, path, resp.status_code, resp.text[:300])
                resp.raise_for_status()
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code}")
            resp.raise_for_status()
            if not resp.content:
                return {}
            ctype = resp.headers.get("Content-Type", "")
            return resp.json() if "json" in ctype else resp.content
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", 0) in NO_RETRY_CODES:
                raise
            last_exc = e
        except requests.RequestException as e:
            last_exc = e
        if attempt < MAX_RETRIES:
            time.sleep(BACKOFF[attempt])
    raise RuntimeError(f"WB {method} {path} failed after retries: {last_exc}")


# --- Marketplace: заказы ---
def orders_new() -> dict:
    return _request("GET", config.WB_API_BASE, "/api/v3/orders/new")


def orders_status(order_ids: list[int]) -> dict:
    return _request("POST", config.WB_API_BASE, "/api/v3/orders/status",
                    json_body={"orders": order_ids})


def confirm_order(order_id: int) -> dict:
    return _request("PATCH", config.WB_API_BASE, f"/api/v3/orders/{order_id}/confirm")


def cancel_order(order_id: int) -> dict:
    return _request("PATCH", config.WB_API_BASE, f"/api/v3/orders/{order_id}/cancel")


def stickers(order_ids: list[int], sticker_type="png", width=58, height=40):
    """Стикеры-этикетки. type: svg|png|zplv|zplh. Возвращает JSON со стикерами."""
    return _request(
        "POST", config.WB_API_BASE, "/api/v3/orders/stickers",
        params={"type": sticker_type, "width": width, "height": height},
        json_body={"orders": order_ids},
    )


# --- Marketplace: остатки ---
def update_stocks(warehouse_id: str, stocks: list[dict]) -> dict:
    """stocks: [{'sku': barcode, 'amount': n}, ...]"""
    return _request("PUT", config.WB_API_BASE, f"/api/v3/stocks/{warehouse_id}",
                    json_body={"stocks": stocks})


# --- Content: карточки ---
def cards_list(cursor=None, limit=100) -> dict:
    body = {"settings": {"cursor": {"limit": limit}, "filter": {"withPhoto": -1}}}
    if cursor:
        body["settings"]["cursor"].update(cursor)
    return _request("POST", config.WB_CONTENT_API_BASE,
                    "/content/v2/get/cards/list", json_body=body)
WBLUTNER_CANON_EOF
cat > main.py <<'WBLUTNER_CANON_EOF'
"""
main.py — Этап 5. Забирает новые FBS-задания WB и создаёт заказы в Lutner.

Идемпотентность: PRIMARY KEY orders.wb_order_id. Уже обработанный — пропускаем.
Флаг --dry-run: делает всё, кроме реального POST /order/ в Lutner.

Cron: */5 * * * * flock -n /tmp/main.lock -c 'cd /opt/wb-lutner && venv/bin/python main.py'
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta

from lib import config, db, wb_api, lutner_api
from lib.logging_setup import get_logger
from lib.mailer import alert

log = get_logger("main")
MSK = timezone(timedelta(hours=3))


def _shipment_date() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def _lookup(conn, barcode: str):
    """barcode WB -> (lutner_article, lutner_uuid, store_spb, store_ekb) или None."""
    row = conn.execute(
        """
        SELECT m.lutner_article AS article, m.lutner_uuid AS uuid,
               COALESCE(s.store_spb,0) AS spb, COALESCE(s.store_ekb,0) AS ekb
        FROM mapping m
        LEFT JOIN stock s ON s.uuid = m.lutner_uuid
        WHERE m.wb_barcode = ?
        """,
        (barcode,),
    ).fetchone()
    return row


def _save(conn, wb_id, status, *, lutner_id=None, comment=None, items=None, error=None):
    conn.execute(
        """
        INSERT INTO orders
            (wb_order_id, lutner_order_id, status, comment, items_json, error_json, created_at)
        VALUES (?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(wb_order_id) DO UPDATE SET
            lutner_order_id=excluded.lutner_order_id, status=excluded.status,
            comment=excluded.comment, items_json=excluded.items_json,
            error_json=excluded.error_json
        """,
        (
            wb_id, lutner_id, status, comment,
            json.dumps(items, ensure_ascii=False) if items is not None else None,
            json.dumps(error, ensure_ascii=False) if error is not None else None,
        ),
    )
    conn.commit()


def process_order(order: dict, dry_run: bool) -> str:
    wb_id = order["id"]
    skus = order.get("skus") or []
    barcode = skus[0] if skus else None
    qty = 1  # WB FBS: как правило 1 позиция/заказ

    conn = db.get_conn()
    try:
        if conn.execute("SELECT 1 FROM orders WHERE wb_order_id=?", (wb_id,)).fetchone():
            return "skip"

        if not barcode:
            _save(conn, wb_id, "failed", error={"reason": "no sku in WB order"})
            alert("WB order без баркода", f"order {wb_id}: {order}")
            return "failed"

        m = _lookup(conn, barcode)
        if not m:
            _save(conn, wb_id, "no_mapping",
                  items={"barcode": barcode}, error={"reason": "barcode not in mapping"})
            alert("Неизвестный баркод", f"order {wb_id}, barcode {barcode} нет в mapping")
            return "no_mapping"

        comment = (
            f"{config.MARKETPLACE_NAME}, отгрузка {_shipment_date()}, "
            f"WB order {wb_id}"
        )
        items = {"article": m["article"], "qty": qty, "barcode": barcode}

        if m["spb"] < qty:
            reason = "ekb_only" if m["ekb"] >= qty else "no_stock"
            _save(conn, wb_id, reason, items=items,
                  error={"spb": m["spb"], "ekb": m["ekb"], "need": qty})
            where = "только в EKB" if reason == "ekb_only" else "нигде"
            alert(f"Нет остатка в SPB ({reason})",
                  f"order {wb_id}, {m['article']}: SPB={m['spb']} EKB={m['ekb']}, "
                  f"нужно {qty}. Наличие: {where}. Ручной разбор.")
            return reason

        if dry_run:
            _save(conn, wb_id, "dry_run", comment=comment, items=items)
            log.info("[dry-run] would create Lutner order for WB %s (%s x%s)",
                     wb_id, m["article"], qty)
            return "dry_run"

        payload = {
            "profile": int(config.LUTNER_PROFILE_ID) if config.LUTNER_PROFILE_ID else None,
            "warehouse": "spb",
            "dropshipping": True,
            "dropshipping_count": 1,
            "items": {m["article"]: qty},
            "comment": comment,
        }
        resp = lutner_api.create_order(payload)
        errors = resp.get("errors") if isinstance(resp, dict) else None
        if errors:
            _save(conn, wb_id, "failed", comment=comment, items=items, error=errors)
            alert("Lutner отклонил заказ", f"order {wb_id}: {errors}")
            return "failed"

        lutner_id = str(resp.get("order_id") or resp.get("id") or "")
        _save(conn, wb_id, "created", lutner_id=lutner_id, comment=comment, items=items)
        log.info("created Lutner order %s for WB %s", lutner_id, wb_id)
        return "created"

    except Exception as e:  # noqa: BLE001
        try:
            _save(conn, wb_id, "failed", error={"exc": str(e)})
        except Exception:
            log.exception("failed to persist error for %s", wb_id)
        log.exception("process_order %s failed", wb_id)
        alert("main.py: ошибка обработки заказа", f"order {wb_id}: {e}")
        return "failed"
    finally:
        conn.close()


def run(dry_run: bool) -> int:
    try:
        data = wb_api.orders_new()
    except Exception as e:  # noqa: BLE001
        log.exception("orders_new failed")
        alert("main.py: WB orders_new упал", str(e))
        return 1

    orders = data.get("orders") or []
    stats: dict[str, int] = {}
    for o in orders:
        r = process_order(o, dry_run)
        stats[r] = stats.get(r, 0) + 1
    log.info("cycle done: fetched=%s %s", len(orders), stats)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="не отправлять реальный POST /order/ в Lutner")
    args = ap.parse_args()
    sys.exit(run(args.dry_run))
WBLUTNER_CANON_EOF
cat > webhook_server.py <<'WBLUTNER_CANON_EOF'
"""
webhook_server.py — Этап 3. Flask-приёмник остатков/цен от Lutner.

- POST /webhook/stock/<секрет> — батч JSON, всегда 200 OK.
- GET  /health — для UptimeRobot / systemd.
- 404 логируются.

Запуск в бою:
  gunicorn --workers 2 --bind 127.0.0.1:8000 --timeout 60 webhook_server:app
"""
from __future__ import annotations

from flask import Flask, jsonify, request

from lib import config, db
from lib.logging_setup import get_logger

log = get_logger("webhook")

config.require("WEBHOOK_SECRET_PATH")
db.init_db()  # идемпотентно — гарантируем схему при старте воркера

app = Flask(__name__)


def _upsert_stock(conn, item: dict) -> None:
    uuid = str(item.get("id") or "").strip()
    if not uuid:
        raise ValueError("item without id")
    conn.execute(
        """
        INSERT INTO stock
            (uuid, quantity, store_spb, store_ekb,
             price_retail, price_diller, price_mp, updated_at)
        VALUES (?,?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(uuid) DO UPDATE SET
            quantity=excluded.quantity,
            store_spb=excluded.store_spb, store_ekb=excluded.store_ekb,
            price_retail=excluded.price_retail,
            price_diller=excluded.price_diller,
            price_mp=excluded.price_mp,
            updated_at=datetime('now')
        """,
        (
            uuid,
            int(item.get("quantity") or 0),
            int(item.get("store_spb") or 0),
            int(item.get("store_ekb") or 0),
            item.get("price_retail"),
            item.get("price_diller"),
            item.get("price_mp"),
        ),
    )


@app.get("/health")
def health():
    return jsonify(status="ok"), 200


@app.post(f"/webhook/stock/{config.WEBHOOK_SECRET_PATH}")
def webhook_stock():
    status, error, items_count = "ok", None, 0
    body = request.get_data(cache=True)
    try:
        payload = request.get_json(force=True, silent=True)
        if not isinstance(payload, list):
            raise ValueError("expected JSON array")
        items_count = len(payload)

        conn = db.get_conn()
        try:
            conn.execute("BEGIN")
            bad = 0
            for item in payload:
                try:
                    _upsert_stock(conn, item)
                except Exception as ie:  # один кривой товар не ломает батч
                    bad += 1
                    log.warning("bad item skipped: %s (%s)", item, ie)
            conn.commit()
            if bad:
                status = f"partial ({bad} skipped)"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        db.set_state("last_stock_webhook", "ok")
    except Exception as e:  # noqa: BLE001 — никогда не отдаём не-200
        status, error = "error", str(e)
        log.exception("webhook processing error")
    finally:
        try:
            c = db.get_conn()
            c.execute(
                """INSERT INTO webhook_log
                   (source_ip, forwarded_for, user_agent, body_size,
                    items_count, status, error)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    request.remote_addr,
                    request.headers.get("X-Forwarded-For"),
                    request.headers.get("User-Agent"),
                    len(body or b""),
                    items_count,
                    status,
                    error,
                ),
            )
            c.commit()
            c.close()
        except Exception:
            log.exception("failed to write webhook_log")

    # Всегда 200 — иначе Lutner ретраит и шлёт дубли.
    return jsonify(status="received"), 200


@app.errorhandler(404)
def not_found(_e):
    log.warning("404 %s %s from %s ua=%s",
                request.method, request.path, request.remote_addr,
                request.headers.get("User-Agent"))
    return jsonify(status="not found"), 404


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
WBLUTNER_CANON_EOF
cat > sync_catalog.py <<'WBLUTNER_CANON_EOF'
"""
sync_catalog.py — Этап 2. Скачивает полный прайс Lutner (CSV, UTF-8) и
обновляет справочник `catalog` (штрих-коды, габариты, вес).

Cron: 0 3 * * *  flock -n /tmp/catalog.lock -c 'cd /opt/wb-lutner && venv/bin/python sync_catalog.py'
"""
from __future__ import annotations

import csv
import io
import sys

import requests

from lib import config, db
from lib.logging_setup import get_logger
from lib.mailer import alert

log = get_logger("sync_catalog")

# CSV поле -> колонка catalog
FIELD = {
    "uuid": "IE_XML_ID",
    "article": "IP_PROP96",
    "barcode": "IP_PROP95",
    "name": "IE_NAME",
    "brand": "IP_PROP114",
    "package_length": "IP_PROP258",
    "package_width": "IP_PROP259",
    "package_height": "IP_PROP260",
    "weight": "IP_PROP106",
    "discontinued": "IP_PROP140",  # пусто = активен
}


def _to_float(v):
    if v is None:
        return None
    v = str(v).strip().replace(",", ".")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _download() -> str:
    config.require("LUTNER_CATALOG_FULL_URL")
    resp = requests.get(config.LUTNER_CATALOG_FULL_URL, timeout=300)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def sync() -> int:
    text = _download()
    reader = csv.DictReader(io.StringIO(text), delimiter=config.CSV_DELIMITER)
    rows = list(reader)

    # Защита от пустого CSV — не обнуляем справочник.
    if not rows:
        raise RuntimeError("CSV пуст (0 строк) — прайс не обновлён, справочник цел")

    conn = db.get_conn()
    try:
        conn.execute("BEGIN")
        conn.execute("UPDATE catalog SET active = 0")
        n = 0
        for row in rows:
            uuid = (row.get(FIELD["uuid"]) or "").strip()
            if not uuid:
                continue
            active = 0 if (row.get(FIELD["discontinued"]) or "").strip() else 1
            conn.execute(
                """
                INSERT INTO catalog
                    (uuid, article, barcode, name, brand,
                     package_length, package_width, package_height, weight,
                     active, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now'))
                ON CONFLICT(uuid) DO UPDATE SET
                    article=excluded.article, barcode=excluded.barcode,
                    name=excluded.name, brand=excluded.brand,
                    package_length=excluded.package_length,
                    package_width=excluded.package_width,
                    package_height=excluded.package_height,
                    weight=excluded.weight, active=excluded.active,
                    updated_at=datetime('now')
                """,
                (
                    uuid,
                    (row.get(FIELD["article"]) or "").strip(),
                    (row.get(FIELD["barcode"]) or "").strip(),
                    (row.get(FIELD["name"]) or "").strip(),
                    (row.get(FIELD["brand"]) or "").strip(),
                    _to_float(row.get(FIELD["package_length"])),
                    _to_float(row.get(FIELD["package_width"])),
                    _to_float(row.get(FIELD["package_height"])),
                    _to_float(row.get(FIELD["weight"])),
                    active,
                ),
            )
            n += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return n


def main() -> int:
    try:
        n = sync()
    except Exception as e:  # noqa: BLE001
        log.exception("catalog sync failed")
        alert("sync_catalog FAILED", f"Синхронизация каталога упала:\n{e}")
        db.set_state("last_catalog_sync", f"error: {e}")
        return 1

    conn = db.get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM catalog").fetchone()["c"]
    with_bc = conn.execute(
        "SELECT COUNT(*) c FROM catalog WHERE barcode != ''"
    ).fetchone()["c"]
    active = conn.execute("SELECT COUNT(*) c FROM catalog WHERE active=1").fetchone()["c"]
    conn.close()

    log.info("catalog synced: rows=%s total=%s barcodes=%s active=%s",
             n, total, with_bc, active)
    db.set_state("last_catalog_sync", "ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
WBLUTNER_CANON_EOF
cat > sync_stock_to_wb.py <<'WBLUTNER_CANON_EOF'
"""
sync_stock_to_wb.py — Этап 5. Каждые 10 мин пушит store_spb в остатки WB.
Нет записи в stock -> 0. Батчами по 1000.

Cron: */10 * * * * flock -n /tmp/stock-sync.lock -c 'cd /opt/wb-lutner && venv/bin/python sync_stock_to_wb.py'
"""
from __future__ import annotations

import sys

from lib import config, db, wb_api
from lib.logging_setup import get_logger
from lib.mailer import alert

log = get_logger("sync_stock_to_wb")
BATCH = 1000


def run() -> int:
    config.require("WB_WAREHOUSE_ID")
    conn = db.get_conn()
    rows = conn.execute(
        """SELECT m.wb_barcode AS sku, COALESCE(s.store_spb,0) AS amount
           FROM mapping m LEFT JOIN stock s ON s.uuid = m.lutner_uuid"""
    ).fetchall()
    conn.close()

    stocks = [{"sku": r["sku"], "amount": int(r["amount"])} for r in rows]
    if not stocks:
        log.info("mapping пуст — нечего пушить")
        return 0

    pushed = 0
    try:
        for i in range(0, len(stocks), BATCH):
            wb_api.update_stocks(config.WB_WAREHOUSE_ID, stocks[i:i + BATCH])
            pushed += len(stocks[i:i + BATCH])
    except Exception as e:  # noqa: BLE001
        log.exception("stock push failed")
        alert("sync_stock_to_wb FAILED", f"Пуш остатков в WB упал:\n{e}")
        return 1

    log.info("pushed %s stocks to WB warehouse %s", pushed, config.WB_WAREHOUSE_ID)
    db.set_state("last_stock_to_wb", "ok")
    return 0


if __name__ == "__main__":
    sys.exit(run())
WBLUTNER_CANON_EOF
cat > check_heartbeat.py <<'WBLUTNER_CANON_EOF'
"""
check_heartbeat.py — Этап 4. Алерт, если stock-webhook молчит >15 минут.

Cron: */10 * * * * flock -n /tmp/heartbeat.lock -c 'cd /opt/wb-lutner && venv/bin/python check_heartbeat.py'
"""
from __future__ import annotations

import sys

from lib import db
from lib.logging_setup import get_logger
from lib.mailer import alert

log = get_logger("heartbeat")
THRESHOLD_MIN = 15


def run() -> int:
    conn = db.get_conn()
    row = conn.execute(
        "SELECT updated_at, (julianday('now') - julianday(updated_at)) * 24 * 60 AS age_min "
        "FROM system_state WHERE key='last_stock_webhook'"
    ).fetchone()
    conn.close()

    if not row:
        log.warning("нет ни одного webhook")
        alert("Webhook НЕ ПРИХОДИЛ НИ РАЗУ", "system_state.last_stock_webhook отсутствует")
        return 1

    age = row["age_min"]
    if age is not None and age > THRESHOLD_MIN:
        log.warning("webhook молчит %.1f мин", age)
        alert("Webhook молчит",
              f"Последний webhook от Lutner был {age:.0f} мин назад "
              f"(порог {THRESHOLD_MIN}). Проверь nginx/gunicorn/сеть Lutner.")
        return 1

    log.info("heartbeat ok (age=%.1f min)", age or 0)
    return 0


if __name__ == "__main__":
    sys.exit(run())
WBLUTNER_CANON_EOF
cat > initial_mapping.py <<'WBLUTNER_CANON_EOF'
"""
initial_mapping.py — Этап 5, одноразово. Тянет карточки WB, матчит по
баркоду с catalog, наполняет mapping. Отчёт: сматчилось / не найдено.

Запуск: venv/bin/python initial_mapping.py
"""
from __future__ import annotations

import sys

from lib import db, wb_api
from lib.logging_setup import get_logger

log = get_logger("initial_mapping")


def _iter_cards():
    cursor = None
    while True:
        data = wb_api.cards_list(cursor=cursor, limit=100)
        cards = data.get("cards", [])
        for c in cards:
            yield c
        cur = data.get("cursor") or {}
        total = cur.get("total", 0)
        if total < 100:
            break
        cursor = {"updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}


def run() -> int:
    conn = db.get_conn()
    matched = unmatched = 0
    unmatched_barcodes = []
    try:
        for card in _iter_cards():
            nm_id = card.get("nmID")
            for size in card.get("sizes", []):
                for barcode in size.get("skus", []):
                    row = conn.execute(
                        "SELECT uuid, article FROM catalog WHERE barcode=? LIMIT 1",
                        (barcode,),
                    ).fetchone()
                    if not row:
                        unmatched += 1
                        unmatched_barcodes.append(barcode)
                        continue
                    conn.execute(
                        """INSERT INTO mapping (wb_barcode, wb_nmid, lutner_uuid, lutner_article)
                           VALUES (?,?,?,?)
                           ON CONFLICT(wb_barcode) DO UPDATE SET
                             wb_nmid=excluded.wb_nmid,
                             lutner_uuid=excluded.lutner_uuid,
                             lutner_article=excluded.lutner_article""",
                        (barcode, nm_id, row["uuid"], row["article"]),
                    )
                    matched += 1
        conn.commit()
    finally:
        conn.close()

    log.info("mapping: matched=%s unmatched=%s", matched, unmatched)
    if unmatched_barcodes:
        log.warning("не найдено в catalog (%s): %s",
                    unmatched, ", ".join(unmatched_barcodes[:50]))
    print(f"Сматчилось: {matched}\nНе найдено: {unmatched}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
WBLUTNER_CANON_EOF
cat > morning_report.py <<'WBLUTNER_CANON_EOF'
"""
morning_report.py — Этап 6. К 7:30 МСК шлёт менеджеру Lutner состав отгрузки
за сутки + этикетки WB. Тема: "{ЮР_ЛИЦО}, {ПЛОЩАДКА}, DD.MM.YYYY".
1 письмо = 1 площадка. Проставляет orders.sent_to_manager_at.

Cron: 30 7 * * * flock -n /tmp/morning.lock -c 'cd /opt/wb-lutner && venv/bin/python morning_report.py'
"""
from __future__ import annotations

import base64
import csv
import io
import sys
from datetime import datetime, timezone, timedelta

from lib import config, db, wb_api
from lib.logging_setup import get_logger
from lib.mailer import send_email, alert

log = get_logger("morning_report")
MSK = timezone(timedelta(hours=3))


def _pending():
    conn = db.get_conn()
    rows = conn.execute(
        """SELECT wb_order_id, lutner_order_id, comment, items_json
           FROM orders
           WHERE status='created' AND sent_to_manager_at IS NULL
             AND created_at > datetime('now','-1 day')
           ORDER BY wb_order_id"""
    ).fetchall()
    conn.close()
    return rows


def _composition_csv(rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["WB order", "Lutner order", "Состав", "Комментарий"])
    for r in rows:
        w.writerow([r["wb_order_id"], r["lutner_order_id"] or "",
                    r["items_json"] or "", r["comment"] or ""])
    return buf.getvalue().encode("utf-8-sig")


def _stickers_pdf(order_ids) -> bytes | None:
    """Собирает этикетки WB в один PDF. Нужен reportlab (мягкий импорт)."""
    try:
        resp = wb_api.stickers(order_ids, sticker_type="png", width=58, height=40)
    except Exception as e:  # noqa: BLE001
        log.warning("stickers fetch failed: %s", e)
        return None
    stickers = resp.get("stickers", []) if isinstance(resp, dict) else []
    if not stickers:
        return None
    try:
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas
    except ImportError:
        log.warning("reportlab не установлен — этикетки не приложены")
        return None

    out = io.BytesIO()
    c = canvas.Canvas(out)
    for s in stickers:
        raw = s.get("file") or s.get("image")
        if not raw:
            continue
        img = ImageReader(io.BytesIO(base64.b64decode(raw)))
        c.setPageSize((165, 115))  # ~58x40mm в пунктах
        c.drawImage(img, 5, 5, width=155, height=105, preserveAspectRatio=True)
        c.showPage()
    c.save()
    return out.getvalue()


def run() -> int:
    config.require("MANAGER_TO")
    rows = _pending()
    if not rows:
        log.info("нет заказов к отгрузке — письмо не отправляем")
        return 0

    date_ru = datetime.now(MSK).strftime("%d.%m.%Y")
    subject = f"{config.LEGAL_ENTITY}, {config.MARKETPLACE_NAME}, {date_ru}"
    body = (
        f"Состав отгрузки {config.MARKETPLACE_NAME} за {date_ru}.\n"
        f"Заказов: {len(rows)}.\n\n"
        f"Детали — в приложенном CSV. Этикетки — в PDF."
    )

    order_ids = [r["wb_order_id"] for r in rows]
    attachments = [("sostav.csv", _composition_csv(rows))]
    pdf = _stickers_pdf(order_ids)
    if pdf:
        attachments.append(("stickers.pdf", pdf))

    ok = send_email(config.MANAGER_TO, subject, body, attachments)
    if not ok:
        alert("morning_report: письмо НЕ отправлено", subject)
        return 1

    conn = db.get_conn()
    conn.executemany(
        "UPDATE orders SET sent_to_manager_at=datetime('now') WHERE wb_order_id=?",
        [(oid,) for oid in order_ids],
    )
    conn.commit()
    conn.close()
    log.info("morning report sent: %s orders, subject='%s'", len(rows), subject)
    return 0


if __name__ == "__main__":
    sys.exit(run())
WBLUTNER_CANON_EOF
cat > daily_summary.py <<'WBLUTNER_CANON_EOF'
"""
daily_summary.py — Этап 6. В 20:00 короткий отчёт себе на ALERT_TO.

Cron: 0 20 * * * flock -n /tmp/summary.lock -c 'cd /opt/wb-lutner && venv/bin/python daily_summary.py'
"""
from __future__ import annotations

import sys

from lib import db
from lib.logging_setup import get_logger
from lib.mailer import send_email
from lib import config

log = get_logger("daily_summary")


def run() -> int:
    conn = db.get_conn()
    by_status = conn.execute(
        """SELECT status, COUNT(*) c FROM orders
           WHERE created_at > datetime('now','-1 day')
           GROUP BY status ORDER BY c DESC"""
    ).fetchall()
    problems = conn.execute(
        """SELECT wb_order_id, status, error_json FROM orders
           WHERE created_at > datetime('now','-1 day')
             AND status IN ('failed','no_stock','ekb_only','no_mapping')
           ORDER BY wb_order_id"""
    ).fetchall()
    conn.close()

    total = sum(r["c"] for r in by_status)
    lines = [f"Заказов за сутки: {total}", ""]
    lines += [f"  {r['status']}: {r['c']}" for r in by_status]
    if problems:
        lines += ["", "Проблемные:"]
        lines += [f"  WB {p['wb_order_id']} [{p['status']}] {p['error_json'] or ''}"
                  for p in problems]

    body = "\n".join(lines)
    to = config.ALERT_TO or config.SMTP_USER or ""
    send_email(to, "[wb-lutner] Вечерний отчёт", body)
    log.info("daily summary sent: total=%s problems=%s", total, len(problems))
    return 0


if __name__ == "__main__":
    sys.exit(run())
WBLUTNER_CANON_EOF
cat > check_balance.py <<'WBLUTNER_CANON_EOF'
"""
check_balance.py — Этап 6. Проверка баланса юрлица в Lutner перед 8:00.

Если у Lutner есть баланс в GET /profile/ — сравниваем с порогом и алертим.
Если endpoint'а баланса нет — просто напоминание проверить вручную.

Cron: 30 6 * * * flock -n /tmp/balance.lock -c 'cd /opt/wb-lutner && venv/bin/python check_balance.py'
"""
from __future__ import annotations

import sys

from lib import config, lutner_api
from lib.logging_setup import get_logger
from lib.mailer import alert

log = get_logger("check_balance")

# TODO(уточнить у Lutner): точное имя поля баланса в /profile/.
BALANCE_KEYS = ("balance", "saldo", "money", "amount")
THRESHOLD = float(config._get("LUTNER_BALANCE_MIN", "0") or 0)


def _extract_balance(profile) -> float | None:
    items = profile if isinstance(profile, list) else [profile]
    for it in items:
        if not isinstance(it, dict):
            continue
        for k in BALANCE_KEYS:
            if k in it:
                try:
                    return float(str(it[k]).replace(",", "."))
                except ValueError:
                    return None
    return None


def run() -> int:
    try:
        profile = lutner_api.get_profiles()
        bal = _extract_balance(profile)
    except Exception as e:  # noqa: BLE001
        log.warning("profile fetch failed: %s", e)
        bal = None

    if bal is None:
        alert("Проверьте баланс Lutner",
              "Автоматически баланс получить не удалось (нет поля/endpoint'а). "
              "Проверьте вручную положительный баланс к 8:00 МСК.")
        log.info("balance unknown -> reminder sent")
        return 0

    if bal <= THRESHOLD:
        alert("НИЗКИЙ баланс Lutner",
              f"Баланс {bal} <= порог {THRESHOLD}. Пополните до 8:00 МСК!")
        log.warning("low balance: %s", bal)
        return 1

    log.info("balance ok: %s", bal)
    return 0


if __name__ == "__main__":
    sys.exit(run())
WBLUTNER_CANON_EOF
echo
echo "Готово. Проверка (заглушек быть не должно):"
wc -l lib/*.py *.py
