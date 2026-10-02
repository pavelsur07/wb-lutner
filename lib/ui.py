"""
lib/ui.py — минимальный read-only UI: списки заказов WB и отправлений Ozon.

- Только просмотр: никаких действий с заказами.
- Доступ: HTTP Basic, один статичный пользователь из .env (UI_USER/UI_PASSWORD).
- Bootstrap 5 через CDN, серверный рендеринг Jinja2 (templates/ui/).

Blueprint регистрируется в webhook_server.py только если заданы
UI_USER и UI_PASSWORD — иначе UI молча отключён, вебхук не страдает.
"""
from __future__ import annotations

import hmac
import json
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, redirect, render_template, request, url_for

from lib import db
from lib import config

bp = Blueprint("ui", __name__, url_prefix="/ui")

PER_PAGE = 50
MSK = timezone(timedelta(hours=3))

# table -> (id column, marketplace title, template endpoint, last-cycle state key)
_TABLES = {
    "wb": ("orders", "wb_order_id", "Wildberries", "ui.wb_orders", "last_cycle_wb"),
    "ozon": ("ozon_orders", "posting_number", "Ozon", "ui.ozon_orders", "last_cycle_ozon"),
}

STATUS_CLASS = {
    "created": "success",
    "manual": "success",
    "dry_run": "secondary",
    "skipped_status": "secondary",
    "skipped_stale": "secondary",
    "pending_supply": "warning",
    "no_mapping": "warning",
    "no_stock": "warning",
    "ekb_only": "warning",
    "failed": "danger",
}


@bp.before_request
def _require_auth():
    a = request.authorization
    ok = (
        a
        and hmac.compare_digest(a.username or "", config.UI_USER or "")
        and hmac.compare_digest(a.password or "", config.UI_PASSWORD or "")
    )
    if not ok:
        return Response(
            "Требуется авторизация\n",
            401,
            {"WWW-Authenticate": 'Basic realm="wb-lutner"'},
        )


def _fmt_dt(value: str | None) -> str:
    """'YYYY-MM-DD HH:MM:SS' (UTC, sqlite datetime('now')) -> МСК."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M")


def _fmt_items(raw: str | None) -> str:
    """items_json -> компактная строка состава заказа."""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return str(raw)
    if isinstance(data, dict):  # WB: {"article": ..., "qty": ..., "barcode": ...}
        s = f"{data.get('article') or '?'} ×{data.get('qty', 1)}"
        if data.get("barcode"):
            s += f" ({data['barcode']})"
        return s
    if isinstance(data, list):  # Ozon: [{"article": ..., "qty": ...}, ...]
        return "; ".join(
            f"{p.get('article') or p.get('offer_id') or '?'} ×{p.get('qty', 1)}"
            for p in data
        )
    return str(data)


def _fmt_error(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except (ValueError, TypeError):
        return str(raw)


@bp.get("/")
def index():
    return redirect(url_for("ui.wb_orders"))


@bp.get("/wb")
def wb_orders():
    return _orders_page("wb")


@bp.get("/ozon")
def ozon_orders():
    return _orders_page("ozon")


def _orders_page(market: str):
    table, id_col, title, endpoint, state_key = _TABLES[market]
    page = max(request.args.get("page", 1, type=int), 1)
    status = (request.args.get("status") or "").strip()

    where, params = "", []
    if status:
        where, params = " WHERE status=?", [status]

    conn = db.get_conn()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table}{where}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM {table}{where} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, PER_PAGE, (page - 1) * PER_PAGE),
        ).fetchall()
        statuses = [
            r["status"]
            for r in conn.execute(
                f"SELECT DISTINCT status FROM {table} ORDER BY status"
            )
        ]
        last_update = conn.execute(
            f"SELECT MAX(created_at) AS m FROM {table}{where}", params
        ).fetchone()["m"]
    finally:
        conn.close()

    lu = _fmt_dt(last_update)
    last_update_date, _, last_update_time = lu.partition(" ")
    lc = _fmt_dt(db.get_state(state_key))
    last_check = f"{lc} МСК" if lc else ""

    orders = []
    for r in rows:
        cols = r.keys()
        orders.append({
            "id": r[id_col],
            "status": r["status"],
            "status_class": STATUS_CLASS.get(r["status"], "secondary"),
            "lutner_id": r["lutner_order_id"] or "—",
            "items_str": _fmt_items(r["items_json"]),
            "comment": r["comment"] or "",
            "error": _fmt_error(r["error_json"]),
            "created": _fmt_dt(r["created_at"]),
            "status_at": _fmt_dt(r["status_at"]) if "status_at" in cols else "",
        })

    pages = max(1, -(-total // PER_PAGE))  # ceil
    return render_template(
        "ui/orders.html",
        active=market,
        endpoint=endpoint,
        marketplace=title,
        orders=orders,
        statuses=statuses,
        status=status,
        page=page,
        pages=pages,
        total=total,
        last_update_date=last_update_date,
        last_update_time=last_update_time,
        last_check=last_check,
    )
