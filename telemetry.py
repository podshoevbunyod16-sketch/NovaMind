"""telemetry.py — сквозная трассировка всех шагов агента.

Зачем
-----
В NovaMind была «трасса шагов» (таблица ``step_traces``): плоский список
строк для панели «Что делал агент». Для отладки этого мало:

* нет времени на шаг — непонятно, где ход завис (модель, поиск, терминал);
* нет связи «шаг → ход → пользователь → модель → версия промпта»;
* нет единого места, где видно решения ограждений, права инструментов и
  подтверждения — они рассыпаны по разным журналам;
* нельзя ответить на вопрос «почему сегодня 30% ходов падают».

Что здесь есть
--------------
Модель спанов, как в OpenTelemetry, но без зависимостей:
``trace`` → набор ``span`` (участков работы) → ``events`` (мгновенные
события внутри спана). Каждый ход агента — одна трасса.

* :class:`Trace` — дерево спанов, атрибуты хода (провайдер, модель, версия
  промпта, capability, вердикты ограждений) и результат.
* :class:`Tracer` — создаёт трассу, открывает спаны, пишет журнал.
* Хранение — JSONL + отдельный файл на трассу в ``TRACE_DIR``
  (по умолчанию ``traces/``), плюс кольцевой буфер в памяти для быстрого
  ответа API. Секреты в атрибутах автоматически вырезаются.
* :func:`metrics` — агрегаты по последним трассам: число ходов, ошибки,
  средняя длительность, самые частые инструменты.

API для интерфейса (см. ``routes/ops.py``):
``GET /api/agent/trace/<trace_id>``, ``GET /api/agent/runs/<run_id>/trace``,
``GET /api/ops/metrics``.

Модуль не делает сетевых вызовов: всё лежит локально, поэтому работает и в
Termux, и на сервере без внешней APM.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

log = logging.getLogger("novamind.telemetry")

TRACE_DIR = os.getenv("TRACE_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "traces")
ENABLED = str(os.getenv("TRACING_ENABLED", "1")).strip() != "0"
MAX_ATTRIBUTE_CHARS = int(os.getenv("TRACE_MAX_ATTRIBUTE", "2000"))
MAX_SPANS = int(os.getenv("TRACE_MAX_SPANS", "400"))
RECENT_LIMIT = int(os.getenv("TRACE_RECENT", "200"))

#: Кольцевой буфер последних трасс. Живёт внутри экземпляра :class:`Tracer`,
#: чтобы тесты (и отдельные подсистемы) не смешивали свои трассы с общими.
_RECENT_LOCK = threading.Lock()
_WRITE_LOCK = threading.Lock()

#: Ключи атрибутов, значения которых нельзя писать на диск.
_SECRET_KEY = re.compile(
    r"(?i)(token|secret|passw|api[_-]?key|authorization|cookie|session|capability|"
    r"bearer|private|credential)")

_SECRET_VALUE = re.compile(
    r"(?i)\b(sk-[A-Za-z0-9_\-]{10,}|gsk_[A-Za-z0-9]{10,}|ghp_[A-Za-z0-9]{10,}|"
    r"AIza[0-9A-Za-z_\-]{20,}|xox[abprs]-[A-Za-z0-9\-]{10,}|"
    r"bearer\s+[A-Za-z0-9._\-]{16,})")


def enabled() -> bool:
    return ENABLED


def _clip(value: Any) -> Any:
    """Обрезает длинные значения и превращает несериализуемое в строку."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_clip(item) for item in list(value)[:40]]
    if isinstance(value, dict):
        return {str(key)[:60]: _clip(val) for key, val in list(value.items())[:40]}
    text = str(value)
    if len(text) > MAX_ATTRIBUTE_CHARS:
        text = text[:MAX_ATTRIBUTE_CHARS] + "…"
    return _SECRET_VALUE.sub("[скрыто]", text)


def sanitize(attributes: dict) -> dict:
    """Вырезает секреты из атрибутов спана/события."""
    result: dict = {}
    for key, value in (attributes or {}).items():
        name = str(key)
        if _SECRET_KEY.search(name):
            result[name] = "[скрыто]"
            continue
        result[name] = _clip(value)
    return result


@dataclass
class Span:
    """Участок работы агента: шаг, инструмент, подтверждение, вызов модели."""

    name: str
    kind: str = "internal"
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_id: str = ""
    started: float = field(default_factory=time.time)
    ended: float | None = None
    status: str = "ok"
    attributes: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)

    def finish(self, status: str = "ok") -> "Span":
        self.ended = time.time()
        self.status = status
        return self

    @property
    def duration_ms(self) -> int:
        end = self.ended if self.ended is not None else time.time()
        return int((end - self.started) * 1000)

    def set_attribute(self, key: str, value: Any) -> "Span":
        self.attributes[str(key)] = value
        return self

    def event(self, name: str, attributes: dict | None = None) -> None:
        self.events.append({"name": name, "at": round(time.time() - self.started, 4),
                            "attributes": sanitize(attributes or {})})

    def as_dict(self) -> dict:
        return {"span_id": self.span_id, "parent_id": self.parent_id,
                "name": self.name, "kind": self.kind, "status": self.status,
                "started": round(self.started, 4),
                "duration_ms": self.duration_ms,
                "attributes": sanitize(self.attributes),
                "events": list(self.events)}


@dataclass
class Trace:
    """Один ход агента: дерево спанов и итог."""

    name: str = "agent.turn"
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started: float = field(default_factory=time.time)
    attributes: dict = field(default_factory=dict)
    spans: list[Span] = field(default_factory=list)
    status: str = "running"
    result: dict = field(default_factory=dict)
    ended: float | None = None

    # ── спаны ────────────────────────────────────────────────────────────
    def span(self, name: str, kind: str = "internal", parent: Span | None = None,
             attributes: dict | None = None) -> Span:
        if len(self.spans) >= MAX_SPANS:
            span = Span(name=name, kind=kind, parent_id=parent.span_id if parent else "")
            span.status = "dropped"
            return span
        span = Span(name=name, kind=kind,
                    parent_id=(parent or self._root_span()).span_id if (parent or self.spans) else "",
                    attributes=dict(attributes or {}))
        self.spans.append(span)
        return span

    def _root_span(self) -> Span | None:
        return self.spans[0] if self.spans else None

    def event(self, name: str, attributes: dict | None = None,
              parent: Span | None = None) -> None:
        target = parent or self._root_span()
        if target is None:
            target = self.span("turn")
        target.event(name, attributes)

    # ── итог ─────────────────────────────────────────────────────────────
    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[str(key)] = value

    def finish(self, status: str = "ok", result: dict | None = None) -> "Trace":
        self.ended = time.time()
        self.status = status
        if result:
            self.result.update(sanitize(result))
        for span in self.spans:
            if span.ended is None and span.status != "dropped":
                span.finish(span.status if span.status != "ok" else "ok")
        return self

    @property
    def duration_ms(self) -> int:
        end = self.ended if self.ended is not None else time.time()
        return int((end - self.started) * 1000)

    def as_dict(self) -> dict:
        spans = [span.as_dict() for span in self.spans]
        return {
            "trace_id": self.trace_id,
            "name": self.name,
            "status": self.status,
            "started": round(self.started, 4),
            "duration_ms": self.duration_ms,
            "attributes": sanitize(self.attributes),
            "result": sanitize(self.result),
            "spans": spans,
            "summary": summarize_spans(spans),
        }


def summarize_spans(spans: Iterable[dict]) -> dict:
    """Короткая сводка для интерфейса: где агент провёл время."""
    tools: dict[str, int] = {}
    errors = 0
    slowest = {"name": "", "duration_ms": 0}
    for span in spans:
        if span.get("kind") == "tool":
            name = str(span.get("name") or "tool")
            tools[name] = tools.get(name, 0) + 1
        if span.get("status") not in ("ok", "dropped"):
            errors += 1
        if int(span.get("duration_ms") or 0) > int(slowest["duration_ms"]):
            slowest = {"name": span.get("name") or "", "duration_ms": int(span["duration_ms"])}
    return {"spans": len(spans) if hasattr(spans, "__len__") else 0,
            "tools": tools, "errors": errors, "slowest": slowest}


class Tracer:
    """Создаёт трассы и сохраняет их. Один экземпляр на процесс."""

    def __init__(self, directory: str | None = None, enabled_flag: bool | None = None,
                 recent_limit: int | None = None):
        self.directory = directory or TRACE_DIR
        self.enabled = ENABLED if enabled_flag is None else enabled_flag
        self.recent_limit = int(recent_limit or RECENT_LIMIT)
        self._recent: deque[dict] = deque(maxlen=self.recent_limit)

    def start(self, name: str = "agent.turn", **attributes) -> Trace:
        trace = Trace(name=name, attributes=dict(attributes))
        if self.enabled:
            trace.span("turn", kind="turn", attributes={"goal_chars": attributes.get("goal_chars")})
        return trace

    def save(self, trace: Trace) -> str:
        """Пишет трассу в JSONL и отдельным файлом. Возвращает путь ('' — выкл.)."""
        if not self.enabled:
            return ""
        payload = trace.as_dict()
        with _RECENT_LOCK:
            self._recent.append(payload)
        try:
            os.makedirs(self.directory, exist_ok=True)
            day = time.strftime("%Y-%m-%d", time.localtime(trace.started))
            path = os.path.join(self.directory, f"{day}.jsonl")
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with _WRITE_LOCK:
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                # Отдельный файл нужен, чтобы не читать весь журнал за день.
                single = os.path.join(self.directory, f"{trace.trace_id}.json")
                with open(single, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False, indent=1))
            return path
        except OSError:
            log.warning("не удалось записать трассу %s", trace.trace_id, exc_info=True)
            return ""

    # ── чтение ───────────────────────────────────────────────────────────
    def get(self, trace_id: str) -> dict | None:
        trace_id = str(trace_id or "").strip()
        if not trace_id or not re.fullmatch(r"[0-9a-f]{8,64}", trace_id):
            return None
        with _RECENT_LOCK:
            for item in reversed(self._recent):
                if item.get("trace_id") == trace_id:
                    return item
        path = os.path.join(self.directory, f"{trace_id}.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def find_by_run(self, run_id: str) -> dict | None:
        """Находит трассу по ``run_id`` агента (атрибут ``run_id``)."""
        run_id = str(run_id or "").strip()
        if not run_id:
            return None
        with _RECENT_LOCK:
            for item in reversed(self._recent):
                attributes = item.get("attributes") or {}
                if str(attributes.get("run_id") or "") == run_id:
                    return item
        try:
            names = sorted((name for name in os.listdir(self.directory)
                            if name.endswith(".json")), reverse=True)[:400]
        except OSError:
            return None
        for name in names:
            record = self.get(name[:-5])
            if record and str((record.get("attributes") or {}).get("run_id")) == run_id:
                return record
        return None

    def recent(self, limit: int = 20) -> list[dict]:
        with _RECENT_LOCK:
            items = list(self._recent)
        return [
            {"trace_id": item.get("trace_id"), "name": item.get("name"),
             "status": item.get("status"), "started": item.get("started"),
             "duration_ms": item.get("duration_ms"),
             "summary": item.get("summary"),
             "attributes": {key: value for key, value in (item.get("attributes") or {}).items()
                            if key in ("run_id", "chat_id", "provider", "model", "engine")}}
            for item in items[-max(1, int(limit)):]][::-1]

    def metrics(self, limit: int = 100) -> dict:
        """Агрегаты по последним трассам: доля ошибок, длительность, инструменты."""
        with _RECENT_LOCK:
            items = list(self._recent)[-max(1, int(limit)):]
        total = len(items)
        if not total:
            return {"turns": 0, "errors": 0, "error_rate": 0.0, "avg_duration_ms": 0,
                    "tools": {}, "offline": 0}
        errors = sum(1 for item in items if item.get("status") not in ("ok", "running"))
        durations = [int(item.get("duration_ms") or 0) for item in items]
        tools: dict[str, int] = {}
        offline = 0
        for item in items:
            for name, count in (item.get("summary") or {}).get("tools", {}).items():
                tools[name] = tools.get(name, 0) + int(count)
            if str((item.get("attributes") or {}).get("provider")) == "local_demo":
                offline += 1
        return {"turns": total, "errors": errors,
                "error_rate": round(errors / total, 3),
                "avg_duration_ms": int(sum(durations) / total),
                "p95_duration_ms": sorted(durations)[max(0, int(total * 0.95) - 1)],
                "tools": dict(sorted(tools.items(), key=lambda item: item[1], reverse=True)[:10]),
                "offline": offline}


TRACER = Tracer()


class span_context:
    """Контекстный менеджер для участка работы.

    .. code-block:: python

        with telemetry.span_context(trace, "tool:web_search", kind="tool") as span:
            result = search(query)
            span.set_attribute("results", len(result))
    """

    def __init__(self, trace: Trace | None, name: str, kind: str = "internal",
                 attributes: dict | None = None, parent: Span | None = None):
        self.trace = trace
        self.name = name
        self.kind = kind
        self.attributes = attributes or {}
        self.parent = parent
        self.span: Span | None = None

    def __enter__(self) -> Span:
        if self.trace is None:
            return Span(name=self.name, kind=self.kind, attributes=self.attributes)
        self.span = self.trace.span(self.name, kind=self.kind, parent=self.parent,
                                    attributes=self.attributes)
        return self.span

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.span is None:
            return
        if exc_type is not None:
            self.span.status = "error"
            self.span.event("exception", {"type": exc_type.__name__, "message": str(exc)})
        self.span.finish(self.span.status)

    def set_attribute(self, key: str, value: Any) -> None:
        if self.span is not None:
            self.span.set_attribute(key, value)


def start_trace(name: str = "agent.turn", **attributes) -> Trace:
    """Короткий путь: новая трасса через глобальный :data:`TRACER`."""
    return TRACER.start(name, **attributes)


def save_trace(trace: Trace) -> str:
    return TRACER.save(trace)


def get_trace(trace_id: str) -> dict | None:
    return TRACER.get(trace_id)


def find_by_run(run_id: str) -> dict | None:
    return TRACER.find_by_run(run_id)


def recent(limit: int = 20) -> list[dict]:
    return TRACER.recent(limit)


def metrics(limit: int = 100) -> dict:
    return TRACER.metrics(limit)


def summary() -> dict:
    """Конфигурация трассировки для диагностики."""
    with _RECENT_LOCK:
        recent_count = len(TRACER._recent)
    return {"enabled": enabled(), "directory": TRACE_DIR,
            "max_spans": MAX_SPANS, "recent": recent_count}
