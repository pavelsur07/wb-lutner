"""
lib/ui.py — минимальный UI: списки заказов WB и отправлений Ozon (только
просмотр) и вкладка «Настройка»: график отгрузок (/ui/settings/shipping)
и подключение Яндекс Маркета (/ui/settings/ym).

- С заказами никаких действий. Запись — только настройки (график выходных,
  подключение Маркета), все POST защищены CSRF-токеном.
- Доступ: HTTP Basic, один статичный пользователь из .env (UI_USER/UI_PASSWORD).
- Bootstrap 5 через CDN, серверный рендеринг Jinja2 (templates/ui/).

Blueprint регистрируется в webhook_server.py только если заданы
UI_USER и UI_PASSWORD — иначе UI молча отключён, вебхук не страдает.
"""
from __future__ import annotations

import hmac
import json
from datetime import datetime, timedelta, timezone

from flask import (Blueprint, Response, abort, redirect, render_template,
                   request, url_for)

from lib import config, db, shipping, ym_api

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


# --- График отгрузок -----------------------------------------------------

_MARKET_TITLES = {"wb": "Wildberries", "ozon": "Ozon"}
_WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _csrf_token() -> str:
    """
    Токен без сессии, считается от пароля UI. Basic-auth браузер шлёт сам, поэтому без
    токена чужая страница могла бы отправить форму от имени вошедшего.
    """
    return hmac.new((config.UI_PASSWORD or "").encode(), b"ui-settings-csrf",
                    "sha256").hexdigest()


def _preview(weekend: dict) -> list[dict]:
    """Куда уйдёт заказ, пришедший после 07:00 в ближайшие Пт/Сб/Вс."""
    today = datetime.now(MSK).date()
    fri = today + timedelta(days=(4 - today.weekday()) % 7)
    rows = []
    for d in (fri, fri + timedelta(days=1), fri + timedelta(days=2)):
        at = datetime(d.year, d.month, d.day, 10, 0, tzinfo=MSK)
        ship = shipping.ship_date(at, weekend)
        rows.append({
            "order": f"{_WD[d.weekday()]} {d:%d.%m} после 07:00",
            "ship": f"{_WD[ship.weekday()]} {ship:%d.%m}",
        })
    return rows


def _updated_at(market: str) -> str:
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT updated_at FROM system_state WHERE key=?",
                           (shipping._key(market),)).fetchone()
    finally:
        conn.close()
    return _fmt_dt(row["updated_at"]) if row else ""


@bp.get("/settings/shipping")
def shipping_settings():
    markets = []
    for m in shipping.MARKETS:
        wk = shipping.get_weekend(m)
        markets.append({
            "key": m,
            "title": _MARKET_TITLES[m],
            "weekend": wk,
            "updated": _updated_at(m),
            # предпросмотр для всех 4 комбинаций — JS показывает нужную
            # ещё до сохранения
            "previews": {
                f"{int(sat)}{int(sun)}": _preview({"sat": sat, "sun": sun})
                for sat in (False, True) for sun in (False, True)
            },
        })
    return render_template(
        "ui/shipping.html",
        active="shipping",
        markets=markets,
        saved=_MARKET_TITLES.get(request.args.get("saved", "")),
        csrf=_csrf_token(),
    )


@bp.post("/settings/shipping/<market>")
def shipping_settings_save(market: str):
    if market not in shipping.MARKETS:
        abort(404)
    if not hmac.compare_digest(request.form.get("csrf", ""), _csrf_token()):
        abort(400)
    shipping.set_weekend(market,
                         sat=request.form.get("sat") == "1",
                         sun=request.form.get("sun") == "1")
    # PRG: после POST — редирект, F5 не отправит форму повторно
    return redirect(url_for("ui.shipping_settings", saved=market))


# --- Яндекс Маркет: подключение ------------------------------------------

def _check_csrf() -> None:
    if not hmac.compare_digest(request.form.get("csrf", ""), _csrf_token()):
        abort(400)


@bp.get("/settings/ym")
def ym_settings():
    s = ym_api.settings()
    chk = ym_api.last_check()
    if chk.get("at"):
        chk["at"] = _fmt_dt(chk["at"])
    return render_template(
        "ui/ym.html",
        active="ym",
        s=s,
        key_masked=ym_api.mask(s["api_key"]),
        chk=chk,
        msg=request.args.get("msg", ""),
        csrf=_csrf_token(),
    )


def _ym_save_form() -> str:
    """Сохраняет присланные поля; возвращает код ошибки для ?msg= или ''."""
    vals = {}
    for f in ym_api.FIELDS:
        if f not in request.form:
            continue  # кнопки «выбрать» шлют только свои поля
        v = request.form[f].strip()
        if f == "api_key" and not v:
            continue  # пустое поле ключа = не менять
        if f != "api_key" and v and not v.isdigit():
            return f"bad_{f}"
        vals[f] = v
    ym_api.save_settings(**vals)
    return ""


@bp.post("/settings/ym")
def ym_settings_save():
    _check_csrf()
    return redirect(url_for("ui.ym_settings", msg=_ym_save_form() or "saved"))


@bp.post("/settings/ym/check")
def ym_settings_check():
    """Сохраняет форму (если прислана) и сразу проверяет — один клик."""
    _check_csrf()
    err = _ym_save_form()
    if not err:
        ym_api.check()
    return redirect(url_for("ui.ym_settings", msg=err or "checked"))
