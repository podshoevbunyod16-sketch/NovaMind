"""
groq_rotation.py — Thread-safe rotation of Groq API keys.
"""
import os
import time
import threading
import logging

logger = logging.getLogger(__name__)
AUTH_FAILURE_THRESHOLD = max(2, int(os.getenv("GROQ_401_FAILURE_THRESHOLD", "3")))

# Daily quota is skipped for 25 hours; short rate limits default to 60 seconds.
GROQ_KEY_COOLDOWN = 25 * 3600
GROQ_RATE_LIMIT_COOLDOWN = 60

GROQ_KEYS = []
for i in range(1, 10):
    key = os.getenv(f"GROQ_API_KEY_{i}", "").strip()
    if key:
        GROQ_KEYS.append({"key": key, "index": i, "exhausted_at": None, "cooldown_seconds": GROQ_RATE_LIMIT_COOLDOWN, "invalid": False, "auth_failures": 0})

# Main key is used only when numbered keys are absent, avoiding duplicate attempts.
if not GROQ_KEYS:
    main_key = os.getenv("GROQ_API_KEY", "").strip()
    if main_key:
        GROQ_KEYS.append({"key": main_key, "index": 0, "exhausted_at": None, "cooldown_seconds": GROQ_RATE_LIMIT_COOLDOWN, "invalid": False, "auth_failures": 0})

groq_key_index = 0
_groq_lock = threading.Lock()

# Последние известные лимиты, которые Groq присылает в заголовках ответа.
# Нужны, чтобы предупредить пользователя ДО того, как ключ уйдёт в кулдаун.
_limits = {}
LIMIT_REQUEST_WARN = 3
LIMIT_TOKEN_WARN = 2000


def _refresh_expired_keys(now):
    for item in GROQ_KEYS:
        stamp = item.get("exhausted_at")
        if stamp is not None and not item.get("invalid", False) and now - stamp >= item.get("cooldown_seconds", GROQ_RATE_LIMIT_COOLDOWN):
            item["exhausted_at"] = None
            logger.info("Groq key #%s available after cooldown", item["index"])


def get_groq_key():
    """Return an available key, or an empty string when every key is unavailable."""
    global groq_key_index
    if not GROQ_KEYS:
        return ""
    with _groq_lock:
        _refresh_expired_keys(time.time())
        for offset in range(len(GROQ_KEYS)):
            idx = (groq_key_index + offset) % len(GROQ_KEYS)
            item = GROQ_KEYS[idx]
            if item.get("exhausted_at") is None and not item.get("invalid", False):
                groq_key_index = idx
                return item["key"]
        return ""


def mark_groq_key_exhausted(permanent=False, cooldown_seconds=GROQ_RATE_LIMIT_COOLDOWN,
                            key=None, auth_failure=False):
    """Cool down a key; disable it permanently only after repeated consecutive 401s."""
    global groq_key_index
    if not GROQ_KEYS:
        return
    with _groq_lock:
        item = next((entry for entry in GROQ_KEYS if key and entry["key"] == key), GROQ_KEYS[groq_key_index])
        if auth_failure:
            item["auth_failures"] = item.get("auth_failures", 0) + 1
            permanent = permanent or item["auth_failures"] >= AUTH_FAILURE_THRESHOLD
        if permanent:
            item["invalid"] = True
            item["exhausted_at"] = None
            logger.error("Groq key #%s disabled after %s consecutive 401s", item["index"], item.get("auth_failures", 0))
        else:
            item["invalid"] = False
            item["exhausted_at"] = time.time()
            item["cooldown_seconds"] = max(1, int(cooldown_seconds or GROQ_RATE_LIMIT_COOLDOWN))
            logger.warning("Groq key #%s cooldown: %s seconds", item["index"], item["cooldown_seconds"])
        failed_idx = GROQ_KEYS.index(item)
        for offset in range(1, len(GROQ_KEYS) + 1):
            idx = (failed_idx + offset) % len(GROQ_KEYS)
            candidate = GROQ_KEYS[idx]
            if candidate["exhausted_at"] is None and not candidate["invalid"]:
                groq_key_index = idx
                logger.info("Rotating to Groq key #%s", candidate["index"])
                return
        logger.warning("No available Groq keys; check credentials and limits")


def mark_groq_key_success(key=None):
    """Reset consecutive authentication failures after a successful request."""
    if not GROQ_KEYS:
        return
    with _groq_lock:
        item = next((entry for entry in GROQ_KEYS if key and entry["key"] == key), None)
        if item:
            item["auth_failures"] = 0


def get_groq_key_status():
    """Return safe status metadata without exposing key material."""
    now = time.time()
    status = []
    with _groq_lock:
        _refresh_expired_keys(now)
        for idx, item in enumerate(GROQ_KEYS):
            if item["invalid"]:
                status.append({"index": item["index"], "active": False, "status": "invalid"})
            elif item["exhausted_at"] is not None:
                remaining = max(0, item.get("cooldown_seconds", GROQ_RATE_LIMIT_COOLDOWN) - (now - item["exhausted_at"]))
                status.append({
                    "index": item["index"], "active": False, "status": "cooldown",
                    "cooldown_remaining_sec": int(remaining),
                    "cooldown_remaining_hr": round(remaining / 3600, 2),
                })
            else:
                status.append({
                    "index": item["index"],
                    "active": idx == groq_key_index,
                    "status": "active",
                })
    return status


# ══════════════════════════════════════════════════════════════
# ОСТАТОК ЛИМИТОВ (бонус Б3)
# ══════════════════════════════════════════════════════════════

def record_limits(headers, key=None):
    """Запоминает x-ratelimit-* из ответа Groq. Заголовки необязательны."""
    if not headers:
        return None
    def number(name):
        try:
            value = headers.get(name)
        except AttributeError:
            return None
        if value is None:
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    snapshot = {
        "remaining_requests": number("x-ratelimit-remaining-requests"),
        "limit_requests": number("x-ratelimit-limit-requests"),
        "remaining_tokens": number("x-ratelimit-remaining-tokens"),
        "limit_tokens": number("x-ratelimit-limit-tokens"),
        "updated_at": time.time(),
    }
    if all(snapshot[name] is None for name in
           ("remaining_requests", "remaining_tokens")):
        return None
    with _groq_lock:
        _limits[key or "__all__"] = snapshot
    return snapshot


def limits_status():
    """Остаток лимитов по всем видимым ключам (без самих ключей)."""
    with _groq_lock:
        items = [dict(snapshot, key=("" if name == "__all__" else "ключ"))
                 for name, snapshot in _limits.items()]
    status = get_groq_key_status()
    active = sum(1 for item in status if item.get("active"))
    available = sum(1 for item in status if item.get("status") == "active")
    return {
        "keys_total": len(status),
        "keys_available": available,
        "active_key": active > 0,
        "limits": items,
        "remaining_tokens": min((item["remaining_tokens"] for item in items
                                 if item["remaining_tokens"] is not None), default=None),
        "remaining_requests": min((item["remaining_requests"] for item in items
                                   if item["remaining_requests"] is not None), default=None),
    }


def limits_warning(status=None):
    """Текст предупреждения для интерфейса или пустая строка."""
    data = status if status is not None else limits_status()
    if data.get("keys_total") and not data.get("keys_available"):
        return "Все ключи Groq исчерпаны — переключитесь на другого провайдера."
    tokens = data.get("remaining_tokens")
    requests = data.get("remaining_requests")
    if tokens is not None and tokens <= LIMIT_TOKEN_WARN:
        return f"У ключа Groq осталось ~{tokens} токенов — скоро лимит."
    if requests is not None and requests <= LIMIT_REQUEST_WARN:
        return f"У ключа Groq осталось {requests} запросов — скоро лимит."
    return ""


def reset_limits():
    """Сброс состояния (тесты)."""
    with _groq_lock:
        _limits.clear()
