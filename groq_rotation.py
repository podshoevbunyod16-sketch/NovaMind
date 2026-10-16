"""
groq_rotation.py — Thread-safe rotation of Groq API keys.
"""
import os
import time
import threading

# Daily quota is skipped for 25 hours; short rate limits default to 60 seconds.
GROQ_KEY_COOLDOWN = 25 * 3600
GROQ_RATE_LIMIT_COOLDOWN = 60

GROQ_KEYS = []
for i in range(1, 10):
    key = os.getenv(f"GROQ_API_KEY_{i}", "").strip()
    if key:
        GROQ_KEYS.append({"key": key, "index": i, "exhausted_at": None, "cooldown_seconds": GROQ_RATE_LIMIT_COOLDOWN, "invalid": False})

# Main key is used only when numbered keys are absent, avoiding duplicate attempts.
if not GROQ_KEYS:
    main_key = os.getenv("GROQ_API_KEY", "").strip()
    if main_key:
        GROQ_KEYS.append({"key": main_key, "index": 0, "exhausted_at": None, "cooldown_seconds": GROQ_RATE_LIMIT_COOLDOWN, "invalid": False})

groq_key_index = 0
_groq_lock = threading.Lock()


def _refresh_expired_keys(now):
    for item in GROQ_KEYS:
        stamp = item.get("exhausted_at")
        if stamp is not None and not item.get("invalid", False) and now - stamp >= item.get("cooldown_seconds", GROQ_RATE_LIMIT_COOLDOWN):
            item["exhausted_at"] = None
            print(f"[Groq Key] Ключ #{item['index']} снова доступен после cooldown")


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


def mark_groq_key_exhausted(permanent=False, cooldown_seconds=GROQ_RATE_LIMIT_COOLDOWN):
    """Mark a key invalid (401) or rate-limited (429) for the supplied cooldown."""
    global groq_key_index
    if not GROQ_KEYS:
        return
    with _groq_lock:
        item = GROQ_KEYS[groq_key_index]
        if permanent:
            item["invalid"] = True
            item["exhausted_at"] = None
            print(f"[Groq Key] Ключ #{item['index']} отклонён (401), отключён до перезапуска/замены")
        else:
            item["invalid"] = False
            item["exhausted_at"] = time.time()
            item["cooldown_seconds"] = max(1, int(cooldown_seconds or GROQ_RATE_LIMIT_COOLDOWN))
            print(f"[Groq Key] Ключ #{item['index']} ограничен, cooldown {item['cooldown_seconds']} сек.")

        for offset in range(1, len(GROQ_KEYS) + 1):
            idx = (groq_key_index + offset) % len(GROQ_KEYS)
            candidate = GROQ_KEYS[idx]
            if candidate["exhausted_at"] is None and not candidate["invalid"]:
                groq_key_index = idx
                print(f"[Groq Key] Переключение на ключ #{candidate['index']}")
                return
        print("[Groq Key] Нет доступных ключей; проверьте ключи и лимиты")


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
