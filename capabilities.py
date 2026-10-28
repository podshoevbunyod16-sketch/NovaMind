"""capabilities.py — криптографическая идентичность инструментов и least-privilege.

Проблема, которую решает модуль
-------------------------------
Права инструментов в NovaMind были *неявными*: агент получал объект-инструмент
и мог его вызвать, потому что «он есть в словаре». Ничто не подтверждало, что
вызов сделан в рамках именно этого хода, этого пользователя и разрешённого
набора действий. Любая утечка ссылки на инструмент (в навык, в MCP-описание,
в текст ответа) давала возможность вызвать его вне контекста запуска.

Модель
------
На каждый ход выпускается **подписанный capability-токен** (HMAC-SHA256):

    nm1.<b64url(claims)>.<b64url(signature)>

claims: субъект-владелец, ``run_id``, роль, точный список разрешённых
инструментов, время выпуска/истечения и одноразовый ``jti``. Подпись
проверяется константным по времени сравнением; истёкший или чужой токен
отвергается.

* **Роль** задаёт максимум: ``guest`` (только поиск, чтение страниц, знания,
  память, медиа), ``user`` (+ рабочая папка, файлы, терминал), ``admin``
  (+ MCP, расписание, удаление).
* **Least-privilege**: токен несёт пересечение «что просили» и «что разрешено
  роли»; всё остальное в него не попадает и вызвать нельзя.
* **Привязка к запуску**: инструмент проверяет ``run_id`` и владельца, поэтому
  токен одного хода не работает в другом.

Использование в коде
--------------------
.. code-block:: python

    token, cap = capabilities.mint(owner="anon:…", run_id=run_id, role="guest",
                                   tools=["web_search", "knowledge_search"])
    capabilities.activate(cap)            # в потоке хода
    ...
    error = capabilities.require("shell")  # None → можно, dict → отказ
    capabilities.deactivate()

Без активированного токена проверка не блокирует ничего: инструменты можно
вызывать напрямую в тестах и в старых путях. Как только ход агента выпустил
токен, все проверки становятся обязательными (``AGENT_CAPABILITIES=0``
отключает это, но тогда в трассу пишется предупреждение).
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

log = logging.getLogger("novamind.capabilities")

VERSION = "nm1"
ISSUER = "novamind"
DEFAULT_TTL = int(os.getenv("AGENT_CAP_TTL", "1800"))
CLOCK_SKEW = 30

#: Роли и максимум их прав. Имена инструментов — как в движке AgenticSeek
#: (``_build_tools``) и в мосте NovaMind (``nova_tools``).
ROLE_SCOPES: dict[str, frozenset[str]] = {
    "guest": frozenset({
        "web_search", "web_open", "knowledge_search", "recall", "remember",
        "forget", "task", "task_done", "skill_create", "image", "audio", "video",
    }),
    "user": frozenset({
        "web_search", "web_open", "knowledge_search", "recall", "remember",
        "forget", "task", "task_done", "skill_create", "image", "audio", "video",
        # рабочая папка
        "terminal", "python", "file_read", "file_write", "file_edit", "file_find",
        "shell", "git", "file_delete",
    }),
    "admin": frozenset({
        "web_search", "web_open", "knowledge_search", "recall", "remember",
        "forget", "task", "task_done", "skill_create", "image", "audio", "video",
        "terminal", "python", "file_read", "file_write", "file_edit", "file_find",
        "shell", "git", "file_delete",
        "mcp_call", "schedule",
    }),
}

#: Инструменты, которым нужна рабочая папка. Проверяются дополнительно:
#: даже с ролью ``user`` вызов требует активного токена именно этого хода.
WORKSPACE_TOOLS = frozenset({"terminal", "python", "file_read", "file_write",
                             "file_edit", "file_find", "shell", "git", "file_delete"})

_ALIASES = {"bash": "terminal", "shell": "terminal", "files": "file_read",
            "file_finder": "file_find"}

#: Секрет процесса. Если ``AGENT_CAP_SECRET`` не задан, генерируется случайный:
#: токены живут только внутри процесса и не переживают перезапуск (это
#: безопасный вариант по умолчанию).
_SECRET_LOCK = threading.Lock()
_SECRET = (os.getenv("AGENT_CAP_SECRET") or "").encode("utf-8")
_SECRET_SOURCE = "env" if _SECRET else "process"
if not _SECRET:
    _SECRET = secrets.token_bytes(32)

_revoked: set[str] = set()
_revoked_lock = threading.Lock()


class CapabilityError(Exception):
    """Токен недействителен. ``reason`` — машинный код для трассы."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason
        self.message = message or reason


@dataclass(frozen=True)
class Capability:
    """Разобранные и проверенные права одного хода."""

    token: str
    owner: str
    run_id: str
    role: str
    tools: frozenset[str]
    issued_at: float
    expires_at: float
    jti: str
    dropped: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)

    def allows(self, tool: str) -> bool:
        return normalize_tool(tool) in self.tools

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) > self.expires_at

    def as_dict(self) -> dict:
        return {"role": self.role, "run_id": self.run_id, "owner": self.owner,
                "tools": sorted(self.tools), "dropped": list(self.dropped),
                "issued_at": self.issued_at, "expires_at": self.expires_at,
                "jti": self.jti}

    def event(self) -> dict:
        """Событие для трассы: без самого токена (он — секрет)."""
        return {"type": "capability", "role": self.role, "tools": sorted(self.tools),
                "expires_in": max(0, int(self.expires_at - time.time())),
                "jti": self.jti, "dropped": list(self.dropped)}


# ── утилиты ───────────────────────────────────────────────────────────────
def normalize_tool(tool: str) -> str:
    name = str(tool or "").strip().lower()
    return _ALIASES.get(name, name)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload: bytes, secret: bytes | None = None) -> str:
    return _b64(hmac.new(secret or _SECRET, payload, hashlib.sha256).digest())


def _owner_digest(owner: str) -> str:
    """Владелец в токене — хэш: сама строка-владелец не нужна получателю."""
    return hashlib.sha256(str(owner or "").encode("utf-8")).hexdigest()[:24]


def scopes_for_role(role: str) -> frozenset[str]:
    return ROLE_SCOPES.get(str(role or "guest"), ROLE_SCOPES["guest"])


def mint(owner: str, run_id: str, role: str = "guest",
         tools: Iterable[str] | None = None,
         ttl: int | None = None, secret: bytes | None = None,
         extra: dict | None = None) -> tuple[str, Capability]:
    """Выпускает capability-токен.

    ``tools`` — что ход реально собрал (движок знает точный список). Итог —
    пересечение с правами роли; отброшенное попадает в ``dropped`` и в трассу,
    чтобы «тихое» расширение прав было видно.
    """
    role = str(role or "guest")
    allowed = scopes_for_role(role)
    requested = {normalize_tool(tool) for tool in (tools or []) if str(tool or "").strip()}
    granted = frozenset(requested & allowed)
    dropped = tuple(sorted(requested - allowed))
    now = time.time()
    ttl = int(ttl if ttl is not None else DEFAULT_TTL)
    claims = {
        "v": 1, "iss": ISSUER, "sub": _owner_digest(owner),
        "run": str(run_id or ""), "role": role,
        "tools": sorted(granted), "iat": now, "exp": now + max(30, ttl),
        "jti": secrets.token_hex(8),
    }
    if extra:
        claims["extra"] = {key: value for key, value in extra.items()
                           if isinstance(value, (str, int, float, bool))}
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    token = f"{VERSION}.{_b64(payload)}.{_sign(payload, secret)}"
    capability = Capability(token=token, owner=str(owner or ""), run_id=str(run_id or ""),
                            role=role, tools=granted, issued_at=now,
                            expires_at=claims["exp"], jti=claims["jti"], dropped=dropped)
    if dropped:
        log.info("capability: роли %s не разрешены инструменты %s", role, ", ".join(dropped))
    return token, capability


def verify(token: str, *, tool: str | None = None, run_id: str | None = None,
           owner: str | None = None, secret: bytes | None = None,
           now: float | None = None) -> Capability:
    """Проверяет токен и (опционально) право на конкретный инструмент.

    Бросает :class:`CapabilityError` с машинной причиной: ``malformed``,
    ``bad_signature``, ``expired``, ``revoked``, ``wrong_run``, ``wrong_owner``,
    ``tool_not_granted``.
    """
    raw = str(token or "")
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != VERSION:
        raise CapabilityError("malformed", "Неверный формат capability-токена")
    header, body, signature = parts
    try:
        payload = _unb64(body)
    except Exception as error:  # noqa: BLE001 — битый base64 = недействительный токен
        raise CapabilityError("malformed", "Нечитаемая полезная нагрузка токена") from error
    expected = _sign(payload, secret)
    if not hmac.compare_digest(expected, signature):
        raise CapabilityError("bad_signature", "Подпись capability-токена не совпадает")
    try:
        claims = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise CapabilityError("malformed", "Токен не содержит JSON") from error
    if claims.get("iss") != ISSUER or claims.get("v") != 1:
        raise CapabilityError("malformed", "Неизвестный издатель токена")

    moment = now or time.time()
    expires = float(claims.get("exp") or 0)
    if moment > expires + CLOCK_SKEW:
        raise CapabilityError("expired", "Срок действия capability-токена истёк")
    if claims.get("iat", 0) > moment + CLOCK_SKEW:
        raise CapabilityError("malformed", "Время выпуска токена в будущем")

    jti = str(claims.get("jti") or "")
    with _revoked_lock:
        if jti and jti in _revoked:
            raise CapabilityError("revoked", "Capability-токен отозван")
    if run_id is not None and str(claims.get("run") or "") != str(run_id):
        raise CapabilityError("wrong_run", "Токен выпущен для другого запуска")
    if owner is not None and str(claims.get("sub") or "") != _owner_digest(owner):
        raise CapabilityError("wrong_owner", "Токен выпущен для другого пользователя")

    capability = Capability(
        token=raw, owner=str(owner or ""), run_id=str(claims.get("run") or ""),
        role=str(claims.get("role") or "guest"),
        tools=frozenset(claims.get("tools") or ()),
        issued_at=float(claims.get("iat") or 0), expires_at=expires, jti=jti,
        extra=dict(claims.get("extra") or {}))
    if tool is not None and not capability.allows(tool):
        raise CapabilityError("tool_not_granted",
                              f"Инструмент «{normalize_tool(tool)}» не входит в права хода")
    return capability


def revoke(token: str) -> bool:
    """Отзывает токен по ``jti`` (например, после «Стоп»)."""
    try:
        capability = verify(token)
    except CapabilityError:
        return False
    with _revoked_lock:
        _revoked.add(capability.jti)
        if len(_revoked) > 5000:                 # долгий аптайм не должен течь
            _revoked.clear()
    return True


# ── контекст хода ─────────────────────────────────────────────────────────
_ACTIVE: contextvars.ContextVar[Capability | None] = contextvars.ContextVar(
    "nova_capability", default=None)


def activate(capability: Capability | None) -> contextvars.Token:
    return _ACTIVE.set(capability)


def deactivate(token: contextvars.Token | None = None) -> None:
    try:
        if token is None:
            _ACTIVE.set(None)
        else:
            _ACTIVE.reset(token)
    except Exception:  # noqa: BLE001 — сброс контекста не должен ломать ход
        _ACTIVE.set(None)


def current() -> Capability | None:
    return _ACTIVE.get()


def enforcement_enabled() -> bool:
    """Проверки включаются, когда есть активный токен и их не отключили явно."""
    return str(os.getenv("AGENT_CAPABILITIES", "1")).strip() != "0" and _ACTIVE.get() is not None


def require(tool: str, arguments: object = None) -> dict | None:
    """Проверка перед вызовом инструмента. ``None`` — можно, dict — отказ.

    Формат отказа совпадает с ошибками инструментов NovaMind, поэтому агент
    получает его как обычное наблюдение и может объяснить пользователю.
    """
    capability = _ACTIVE.get()
    if capability is None:
        return None
    name = normalize_tool(tool)
    if not enforcement_enabled():
        log.warning("capability: проверки отключены (AGENT_CAPABILITIES=0), вызов %s", name)
        return None
    if capability.expired():
        return _denied(name, "expired", "Срок действия прав на инструменты истёк")
    if not capability.allows(name):
        return _denied(name, "tool_not_granted",
                       f"Инструмент «{name}» не входит в права этого хода")


def _denied(tool: str, kind: str, message: str) -> dict:
    log.warning("capability: отказ tool=%s reason=%s", tool, kind)
    return {"ok": False, "tool": tool,
            "error": {"type": kind, "message": message, "retryable": False}}


def summary() -> dict:
    """Состояние для диагностики: без секретов и токенов."""
    capability = _ACTIVE.get()
    return {
        "enforcement": str(os.getenv("AGENT_CAPABILITIES", "1")).strip() != "0",
        "secret_source": _SECRET_SOURCE,
        "ttl": DEFAULT_TTL,
        "roles": {role: sorted(scopes) for role, scopes in ROLE_SCOPES.items()},
        "active": capability.event() if capability else None,
    }
