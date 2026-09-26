"""
groq_rotation.py — Ротация Groq API ключей с thread-safe защитой
"""
import os
import time
import threading

# Загружаем все GROQ_KEY_* из окружения
GROQ_KEYS = []
GROQ_KEY_COOLDOWN = 25 * 3600  # 25 часов

# --- Загрузка всех Groq ключей ---
GROQ_KEYS = []
for i in range(1, 10):  # GROQ_API_KEY_1 ... GROQ_API_KEY_9
    k = os.getenv(f"GROQ_API_KEY_{i}", "")
    if k:
        GROQ_KEYS.append({"key": k, "index": i, "exhausted_at": None})

# Fallback: если нет нумерованных — берём основной
if not GROQ_KEYS:
    main_key = os.getenv("GROQ_API_KEY", "")
    if main_key:
        GROQ_KEYS.append({"key": main_key, "index": 0, "exhausted_at": None})

groq_key_index = 0  # Текущий активный ключ
_groq_lock = threading.Lock()  # FIX: защита от race condition

# Cooldown: 1 день + 1 час = 90000 секунд
GROQ_KEY_COOLDOWN = 90000


_groq_lock = threading.Lock()
groq_key_index = 0

def get_groq_key():
    """Возвращает текущий активный Groq API ключ"""
    global groq_key_index, GROQ_KEYS

    if not GROQ_KEYS:
        return ""

    with _groq_lock:  # FIX: thread-safe
        now = time.time()
        for key_info in GROQ_KEYS:
            if key_info["exhausted_at"] and (now - key_info["exhausted_at"]) >= GROQ_KEY_COOLDOWN:
                key_info["exhausted_at"] = None
                print(f"[Groq Key] Ключ #{key_info['index']} восстановлен (cooldown истёк)")

        for i in range(len(GROQ_KEYS)):
            idx = (groq_key_index + i) % len(GROQ_KEYS)
            if GROQ_KEYS[idx]["exhausted_at"] is None:
                groq_key_index = idx
                return GROQ_KEYS[idx]["key"]

        return GROQ_KEYS[groq_key_index]["key"]


def mark_groq_key_exhausted(permanent=False):
    """Помечает текущий ключ как исчерпанный. permanent=True для 401 (неверный ключ)."""
    global groq_key_index, GROQ_KEYS

    if not GROQ_KEYS:
        return

    with _groq_lock:  # FIX: thread-safe
        current_key = GROQ_KEYS[groq_key_index]
        if permanent:
            # FIX: 401 = неверный ключ навсегда, cooldown не поможет
            current_key["exhausted_at"] = float('inf')
            print(f"[Groq Key] Ключ #{current_key['index']} неверный (401), отключён навсегда")
        else:
            current_key["exhausted_at"] = time.time()
            print(f"[Groq Key] Ключ #{current_key['index']} исчерпан, cooldown на 25 часов")

        found = False
        for i in range(1, len(GROQ_KEYS)):
            idx = (groq_key_index + i) % len(GROQ_KEYS)
            if GROQ_KEYS[idx]["exhausted_at"] is None:
                groq_key_index = idx
                found = True
                print(f"[Groq Key] Переключение на ключ #{GROQ_KEYS[idx]['index']}")
                break

        if not found:
            print("[Groq Key] ВСЕ ключи исчерпаны! Ожидание восстановления...")


def get_groq_key_status():
    """Возвращает статус всех ключей"""
    now = time.time()
    status = []
    for key_info in GROQ_KEYS:
        if key_info["exhausted_at"] is None:
            status.append({
                "index": key_info["index"],
                "active": (GROQ_KEYS.index(key_info) == groq_key_index),
                "status": "active"
            })
        else:
            remaining = max(0, GROQ_KEY_COOLDOWN - (now - key_info["exhausted_at"]))
            status.append({
                "index": key_info["index"],
                "active": False,
                "status": "cooldown",
                "cooldown_remaining_sec": int(remaining),
                "cooldown_remaining_hr": round(remaining / 3600, 2)
            })
    return status
