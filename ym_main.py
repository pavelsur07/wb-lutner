"""
ym_main.py — забирает новые заказы FBS нашего магазина Яндекс Маркета и создаёт
соответствующие заказы в Lutner. По образцу ozon_main.py.

Отличия:
  - кабинет общий на несколько магазинов -> запрос фильтруется по campaign_id
    из настроек (UI: Настройка → Яндекс Маркет)
  - в заказе может быть НЕСКОЛЬКО позиций -> один заказ Lutner со всеми товарами
  - тестовые заказы Маркета (fake) заказом в Lutner НЕ становятся
  - offerId -> артикул Lutner через ym_mapping

Берём status=PROCESSING с substatus STARTED (ждёт сборки) или READY_TO_SHIP
(собран); остальное — товар ушёл без нас или ещё не оплачен, только фиксируем.
Дата отгрузки — lib/shipping.py (дедлайн 7:00 МСК, график выходных «ym»).
Если Маркет ждёт отгрузку РАНЬШЕ нашей даты — warning в лог.
Комментарий: "Яндекс Маркет, отгрузка DD.MM.YYYY, Заказ N"

Идемпотентность: PRIMARY KEY ym_orders.ym_order_id.

Cron: */5 * * * * flock -n /tmp/ym-main.lock -c 'cd /opt/wb-lutner && venv/bin/python ym_main.py'
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from lib import config, db, lutner_api, shipping, ym_api
from lib.logging_setup import get_logger
from lib.mailer import alert

log = get_logger("ym_main")

PROCESSABLE_SUBSTATUSES = {"STARTED", "READY_TO_SHIP"}
STALE_HOURS = 24


def _save(conn, order_id, status, *, lutner_id=None, comment=None,
          items=None, error=None):
    conn.execute(
        """INSERT INTO ym_orders
               (ym_order_id, lutner_order_id, status, comment,
                items_json, error_json, created_at, status_at)
           VALUES (?,?,?,?,?,?, datetime('now'), datetime('now'))
           ON CONFLICT(ym_order_id) DO UPDATE SET
               lutner_order_id=excluded.lutner_order_id,
               status=excluded.status, comment=excluded.comment,
               items_json=excluded.items_json, error_json=excluded.error_json,
               status_at=datetime('now')""",
        (order_id, lutner_id, status, comment,
         json.dumps(items, ensure_ascii=False) if items is not None else None,
         json.dumps(error, ensure_ascii=False) if error is not None else None),
    )
    conn.commit()


def _shipment_date(order: dict) -> str:
    ours = shipping.shipment_date_str("ym", order.get("creationDate"), log)
    ym = ((order.get("delivery") or {}).get("shipment") or {}).get("shipmentDate")
    if ym:
        try:
            if datetime.strptime(ym, "%Y-%m-%d").date() < datetime.strptime(ours, "%d.%m.%Y").date():
                log.warning("заказ %s: Маркет ждёт отгрузку %s, а по нашему правилу %s",
                            order.get("orderId"), ym, ours)
        except ValueError:
            pass
    return ours


def process_order(order: dict, dry_run: bool) -> str:
    oid = order.get("orderId")
    products = order.get("items") or []

    conn = db.get_conn()
    try:
        if conn.execute("SELECT 1 FROM ym_orders WHERE ym_order_id=?", (oid,)).fetchone():
            return "skip"

        if not products:
            _save(conn, oid, "failed", error={"reason": "no items in order"})
            alert("Яндекс Маркет: заказ без товаров", f"заказ {oid}")
            return "failed"

        lutner_items: dict[str, int] = {}
        items_info, problems = [], []
        for p in products:
            offer_id = (p.get("offerId") or "").strip()
            qty = int(p.get("count") or 1)
            row = conn.execute(
                """SELECT m.lutner_article AS article,
                          COALESCE(s.store_spb,0) AS spb, COALESCE(s.store_ekb,0) AS ekb
                   FROM ym_mapping m LEFT JOIN stock s ON s.uuid = m.lutner_uuid
                   WHERE m.offer_id = ? AND m.lutner_uuid IS NOT NULL""",
                (offer_id,),
            ).fetchone()
            if not row:
                problems.append({"offer_id": offer_id, "reason": "no_mapping"})
                continue
            if row["spb"] < qty:
                problems.append({"offer_id": offer_id, "article": row["article"],
                                 "reason": "no_stock_spb",
                                 "spb": row["spb"], "ekb": row["ekb"], "need": qty})
                continue
            lutner_items[row["article"]] = lutner_items.get(row["article"], 0) + qty
            items_info.append({"offer_id": offer_id, "article": row["article"], "qty": qty})

        if problems:
            _save(conn, oid, "failed", items=items_info, error=problems)
            alert("Яндекс Маркет: заказ не создан", f"заказ {oid}: проблемные позиции:\n{problems}")
            return "failed"

        comment = f"Яндекс Маркет, отгрузка {_shipment_date(order)}, Заказ {oid}"

        if dry_run:
            # dry-run НЕ пишет в ym_orders — иначе реальный прогон заказ пропустит
            log.info("[dry-run] would create Lutner order for YM %s (%s) — %s",
                     oid, lutner_items, comment)
            return "dry_run"

        payload = {
            "profile": int(config.LUTNER_PROFILE_ID) if config.LUTNER_PROFILE_ID else None,
            "warehouse": config.LUTNER_WAREHOUSE,
            "dropshipping": True,
            "dropshipping_count": 1,
            "items": lutner_items,
            "comment": comment,
        }
        resp = lutner_api.create_order(payload)
        errors = resp.get("errors") if isinstance(resp, dict) else None
        if errors:
            _save(conn, oid, "failed", comment=comment, items=items_info, error=errors)
            alert("Lutner отклонил заказ (Яндекс Маркет)", f"заказ {oid}: {errors}")
            return "failed"

        lutner_id = str(resp.get("order_id") or resp.get("id") or "")
        _save(conn, oid, "created", lutner_id=lutner_id, comment=comment, items=items_info)
        log.info("created Lutner order %s for YM order %s", lutner_id, oid)
        return "created"

    except Exception as e:  # noqa: BLE001
        try:
            _save(conn, oid, "failed", error={"exc": str(e)})
        except Exception:
            log.exception("не удалось записать ошибку по %s", oid)
        log.exception("process_order %s failed", oid)
        alert("ym_main: ошибка обработки", f"заказ {oid}: {e}")
        return "failed"
    finally:
        conn.close()


def _age_hours(order: dict) -> float | None:
    try:
        dt = datetime.fromisoformat(str(order.get("creationDate")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def run(dry_run: bool) -> int:
    s = ym_api.settings()
    if not (s["business_id"] and s["campaign_id"]):
        log.error("не заданы businessId / campaignId (UI: Настройка → Яндекс Маркет)")
        return 1

    try:
        orders = ym_api.orders(s["business_id"], s["campaign_id"])
    except Exception as e:  # noqa: BLE001
        log.exception("ym orders fetch failed")
        alert("ym_main: не удалось получить заказы", str(e))
        return 1

    # API уже фильтрует по магазину, но кабинет общий — проверяем ещё раз.
    orders = [o for o in orders if str(o.get("campaignId")) == s["campaign_id"]]

    conn = db.get_conn()
    try:
        known = {r[0] for r in conn.execute("SELECT ym_order_id FROM ym_orders")}
    finally:
        conn.close()

    fresh, skipped, stale = [], [], []
    for o in orders:
        oid = o.get("orderId")
        if not oid or oid in known:
            continue
        if o.get("fake"):
            skipped.append((o, "fake (тестовый заказ Маркета)"))
        elif (o.get("substatus") or "") not in PROCESSABLE_SUBSTATUSES:
            skipped.append((o, f"status={o.get('status')}/{o.get('substatus')}"))
        elif (_age_hours(o) or 0) > STALE_HOURS:
            stale.append(o)
        else:
            fresh.append(o)

    if (skipped or stale) and not dry_run:
        conn = db.get_conn()
        try:
            for o, why in skipped:
                _save(conn, o["orderId"], "skipped_status", comment=why,
                      items=[{"offer_id": i.get("offerId"), "qty": i.get("count")}
                             for i in o.get("items") or []])
            for o in stale:
                _save(conn, o["orderId"], "skipped_stale",
                      comment=f"creationDate={o.get('creationDate')}")
        finally:
            conn.close()
        log.info("пропущено без создания заказов: по статусу/тестовых=%s, старые=%s",
                 len(skipped), len(stale))
    if stale and not dry_run:
        alert("Яндекс Маркет: пропущены старые заказы",
              f"{len(stale)} шт. старше {STALE_HOURS}ч — заказы НЕ создавались, "
              "проверьте вручную:\n" + "\n".join(str(o["orderId"]) for o in stale))

    stats: dict[str, int] = {"skipped_status": len(skipped), "skipped_stale": len(stale)}
    for o in fresh:
        r = process_order(o, dry_run)
        stats[r] = stats.get(r, 0) + 1
    log.info("cycle done: fetched=%s %s", len(orders), stats)
    if not dry_run:
        db.set_state("last_cycle_ym", datetime.now(timezone.utc).isoformat())
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="не создавать заказы в Lutner и ничего не писать в ym_orders")
    args = ap.parse_args()
    sys.exit(run(args.dry_run))
