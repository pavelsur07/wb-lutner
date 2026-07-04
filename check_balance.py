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
