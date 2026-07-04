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
