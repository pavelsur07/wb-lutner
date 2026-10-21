"""
lib/stock_rules.py — какой остаток отдаём маркетплейсам (WB и Ozon).

Пороги из .env (единые для обеих площадок):
- MIN_STOCK_THRESHOLD (по умолчанию 5): меньше — отдаём 0, страховка от
  оверселла: пара штук может уйти в другом канале раньше, чем мы отгрузим.
- MAX_STOCK_CAP (по умолчанию 20): больше — отдаём ровно столько. Маркетплейс
  сам включает товар в акции со сниженной ценой; потолок ограничивает, сколько
  можно продать по невыгодной цене.
"""
from __future__ import annotations

from lib import config

MIN_STOCK = int(config._get("MIN_STOCK_THRESHOLD", "5") or 5)
MAX_STOCK = int(config._get("MAX_STOCK_CAP", "20") or 20)


def sellable(raw) -> int:
    """
    Остаток для маркетплейса по остатку склада Lutner.

    Отрицательные значения Lutner обрезаем нулём (WB отклоняет весь батч).
    """
    amount = max(0, int(raw))
    if amount < MIN_STOCK:
        return 0
    return min(amount, MAX_STOCK)
