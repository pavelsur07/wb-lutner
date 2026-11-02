"""
lib/shipping.py — правило даты отгрузки, общее для WB и Ozon.

- Заказ до 07:00 МСК включительно -> отгрузка в тот же день,
  после 07:00 -> на следующий день.
- Если день нерабочий — переносим на ближайший рабочий.
- Пн–Пт рабочие всегда. Субботу и воскресенье можно сделать рабочими
  ОТДЕЛЬНО для каждой площадки (UI: /ui/settings/shipping).
  Хранится в system_state: ship_weekend_<market> = {"sat": bool, "sun": bool}.
  Нет записи -> оба выходных нерабочие (поведение до настройки).
- Праздники не учитываются.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from lib import db

MSK = timezone(timedelta(hours=3))
CUTOFF = (7, 0)
MARKETS = ("wb", "ozon")
SAT, SUN = 5, 6


def _key(market: str) -> str:
    if market not in MARKETS:
        raise ValueError(f"неизвестная площадка: {market}")
    return f"ship_weekend_{market}"


def get_weekend(market: str) -> dict:
    """{'sat': bool, 'sun': bool} — рабочие ли выходные у площадки."""
    raw = db.get_state(_key(market))
    try:
        data = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        data = {}
    return {"sat": bool(data.get("sat")), "sun": bool(data.get("sun"))}


def set_weekend(market: str, sat: bool, sun: bool) -> None:
    db.set_state(_key(market), json.dumps({"sat": bool(sat), "sun": bool(sun)}))


def working_days(weekend: dict) -> set[int]:
    days = {0, 1, 2, 3, 4}
    if weekend.get("sat"):
        days.add(SAT)
    if weekend.get("sun"):
        days.add(SUN)
    return days


def ship_date(dt: datetime, weekend: dict) -> date:
    """Дата отгрузки для момента заказа dt (приводится к МСК)."""
    dt = dt.astimezone(MSK)
    ship = dt.date()
    if (dt.hour, dt.minute) > CUTOFF:
        ship += timedelta(days=1)
    days = working_days(weekend)
    while ship.weekday() not in days:
        ship += timedelta(days=1)
    return ship


def shipment_date_str(market: str, created, log=None) -> str:
    """
    created — время заказа на площадке (ISO, UTC). Не разобрали/нет —
    берём текущее время. Возвращает 'DD.MM.YYYY'.
    """
    dt = None
    if created:
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            if log:
                log.warning("не разобрал дату заказа=%r, беру текущее время", created)
    if dt is None:
        dt = datetime.now(MSK)
    return ship_date(dt, get_weekend(market)).strftime("%d.%m.%Y")
