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
LUTNER_WAREHOUSE = _get("LUTNER_WAREHOUSE", "spb")  # 'spb' ЛИБО UUID склада из /profile/
LUTNER_CATALOG_FULL_URL = _get("LUTNER_CATALOG_FULL_URL")
LUTNER_CATALOG_SHORT_URL = _get("LUTNER_CATALOG_SHORT_URL")
CSV_DELIMITER = _get("CSV_DELIMITER", ";")
if CSV_DELIMITER in (r"\t", "\\t", "tab", "TAB"):  # в .env таб удобнее писать как \t
    CSV_DELIMITER = "\t"

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
