"""lib/lutner_api.py — обёртки Lutner Rest API + rate limit. Полностью — Этап 5."""
# TODO(Этап 5): class RateLimiter (скользящее окно, thread-safe Lock)
# TODO(Этап 5): get_profiles(), create_order(...), cancel_order(...)
#               retry 2/4/8s, лимит 8/60с, таймаут 30с, не ретраить 400/401/403
