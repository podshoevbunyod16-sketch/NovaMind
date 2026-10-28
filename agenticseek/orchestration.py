"""agenticseek/orchestration.py — паттерны мультиагентной оркестрации.

Зачем
-----
Движок NovaMind умел ровно один сценарий: выбрать ОДНОГО исполнителя по
маршруту и отдать ему запрос. Для сложной задачи это ограничение:

* «исследуй и напиши» требует двух разных ролей — сбор фактов и текст;
* «сравни три варианта» распадается на независимые ветки, которые дешевле
  выполнять параллельно, а не последовательно;
* качество ответа не проверяется: первая же формулировка модели уходит
  пользователю, даже если она противоречит данным;
* при отказе одного исполнителя нет запасного маршрута.

Здесь реализованы четыре паттерна поверх одного и того же вызова модели
(``complete``), поэтому они не зависят от провайдера и тестируются без сети:

``sequential``  планировщик → исполнитель → редактор; каждый видит результат
                предыдущего. Классический конвейер для «сделай N шагов».
``parallel``    несколько ролей работают над запросом независимо
                (исследователь, аналитик), затем синтезатор сводит результаты.
                Ускоряет сбор фактов и даёт разные точки зрения.
``critic``      черновик → критик → правка, до ``max_rounds``. Критик обязан
                вернуть «ОК», иначе замечания уходят автору. Это и есть
                регрессионный контроль качества ответа.
``router``      цепочка маршрутов с деградацией: если первый исполнитель
                вернул пустоту/ошибку, пробуем следующий.

Все паттерны возвращают :class:`OrchestrationResult`: итоговый ответ, шаги
каждой роли, использованный паттерн и заметки (для трассы).

Включается переменной ``AGENT_PATTERN`` (``off`` по умолчанию — прежнее
поведение; ``auto`` — выбор паттерна по запросу; ``sequential`` / ``parallel``
/ ``critic`` — принудительно) и только для запросов, которые движок счёл
сложными (``router.looks_complex``).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

log = logging.getLogger("agenticseek.orchestration")

PATTERNS = ("off", "auto", "sequential", "parallel", "critic", "router")

#: Роли и их системные инструкции. Короткие и разные: одна и та же роль,
#: переписанная своими словами, даёт тот же результат за большие деньги.
ROLES: dict[str, str] = {
    "planner": (
        "Ты — планировщик. Разбей задачу на 3–6 конкретных шагов и укажи, какие "
        "данные нужны для каждого. Не отвечай на саму задачу — только план, "
        "кратко, по-русски, нумерованным списком."),
    "researcher": (
        "Ты — исследователь. Собери проверяемые факты по задаче: числа, даты, "
        "названия, ограничения. Отделяй факт от предположения, отмечай, чего не "
        "хватает. По-русски, списком, без вступлений."),
    "analyst": (
        "Ты — аналитик. Разбери задачу по критериям: варианты, плюсы/минусы, "
        "риски, стоимость. Если данных мало — скажи, каких именно. По-русски."),
    "writer": (
        "Ты — редактор ответа. Собери из материалов один связный ответ "
        "пользователю: коротко, по делу, с примерами, без служебных пояснений "
        "и без упоминаний ролей. По-русски."),
    "critic": (
        "Ты — строгий рецензент. Проверь ответ на фактические ошибки, "
        "противоречия, пропущенные шаги и воду. Если всё в порядке, первой "
        "строкой напиши ровно «ОК», затем короткое обоснование. Иначе перечисли "
        "замечания списком и предложи, что исправить."),
    "verifier": (
        "Ты — проверяющий. Сверь утверждения ответа с материалами. Для каждого "
        "утверждения отметь: подтверждено / не подтверждено / противоречит. "
        "В конце дай вердикт: «ОК» или список исправлений."),
}

#: Признаки запроса, под который паттерн выбирается автоматически.
_COMPARE = re.compile(r"(?i)\b(?:сравн\w*|проанализируй|альтернатив\w*|плюсы\s+и\s+минусы|"
                      r"compare|versus|vs\.?)\b")
_REVIEW = re.compile(r"(?i)\b(?:проверь|отревью\w*|оцени|раскритикуй|улучши|"
                     r"отредактируй|review|improve|critique)\b")
_RESEARCH = re.compile(r"(?i)\b(?:исследуй|найди|собери|выясни|research|gather|"
                       r"разберись|разбери)\b")
_MULTI = re.compile(r"(?i)(?:\?[^?]*\?)|(?:\n\s*\d+[.)])|(?:\bво-первых\b)")

_OK = re.compile(r"(?i)^\s*(?:ок|ok|хорошо|принято|замечаний нет|нет замечаний)")


@dataclass
class RoleResult:
    """Результат одной роли."""

    role: str
    text: str = ""
    ok: bool = True
    error: str = ""
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {"role": self.role, "ok": self.ok, "chars": len(self.text),
                "seconds": round(self.seconds, 2), "error": self.error}


@dataclass
class OrchestrationResult:
    """Итог паттерна: ответ + что делала каждая роль."""

    answer: str
    pattern: str
    steps: list[RoleResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    verdict: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.answer.strip())

    def as_dict(self) -> dict:
        return {"pattern": self.pattern, "ok": self.ok, "chars": len(self.answer),
                "steps": [step.as_dict() for step in self.steps],
                "notes": list(self.notes), "verdict": self.verdict}


Complete = Callable[..., object]


def _extract(result) -> str:
    """``complete`` возвращает ``(text, meta)`` или строку — понимаем оба."""
    if isinstance(result, tuple):
        return str(result[0] or "").strip()
    return str(result or "").strip()


def _ask(complete: Complete, role: str, goal: str, *, context: str = "",
         history: Sequence[dict] | None = None, emit: Callable | None = None,
         timeout: float = 90.0, max_tokens: int = 1200) -> RoleResult:
    """Один вызов модели в роли ``role``. Ошибки не роняют паттерн."""
    started = time.monotonic()
    messages: list[dict] = []
    for item in (history or [])[-4:]:
        if isinstance(item, dict) and item.get("content"):
            messages.append({"role": str(item.get("role") or "user"),
                             "content": str(item["content"])[:2000]})
    content = f"Задача пользователя:\n{goal.strip()}"
    if context.strip():
        content += f"\n\nМатериалы предыдущих ролей (используй их, не пересказывай):\n{context.strip()}"
    messages.append({"role": "user", "content": content})
    if emit:
        emit({"type": "step", "icon": _ICONS.get(role, "🤖"),
              "text": f"Роль «{role}»: работаю над задачей"})
    try:
        text = _extract(complete(messages, system=ROLES[role], temperature=0.4,
                                 max_tokens=max_tokens, timeout=timeout))
    except Exception as error:                     # noqa: BLE001 — паттерн продолжается
        log.warning("orchestration: роль %s упала: %s", role, error)
        result = RoleResult(role=role, ok=False, error=f"{type(error).__name__}: {error}",
                            seconds=time.monotonic() - started)
        if emit:
            emit({"type": "step", "icon": "⚠️",
                  "text": f"Роль «{role}» не справилась: {type(error).__name__}"})
        return result
    result = RoleResult(role=role, text=text, ok=bool(text),
                        seconds=time.monotonic() - started)
    if emit:
        emit({"type": "step", "icon": "✅" if text else "⚠️",
              "text": f"Роль «{role}» закончила" + ("" if text else " без текста")})
    return result


_ICONS = {"planner": "🗺", "researcher": "🔎", "analyst": "📊",
          "writer": "✍️", "critic": "🧐", "verifier": "✔️"}


def _context_of(results: Iterable[RoleResult], limit: int = 2600) -> str:
    parts = [f"[{item.role}]\n{item.text}" for item in results if item.text.strip()]
    text = "\n\n".join(parts)
    if len(text) > limit:
        text = text[-limit:]
    return text


def sequential(complete: Complete, goal: str, *,
               roles: Sequence[str] = ("planner", "writer"),
               history: Sequence[dict] | None = None,
               emit: Callable | None = None) -> OrchestrationResult:
    """Конвейер: каждая роль видит результат предыдущей."""
    steps: list[RoleResult] = []
    notes = ["последовательно: " + " → ".join(roles)]
    context = ""
    for role in roles:
        result = _ask(complete, role, goal, context=context, history=history, emit=emit)
        steps.append(result)
        if not result.text.strip():
            notes.append(f"роль {role} не дала текста")
            continue
        context = _context_of([*steps])
    answer = next((step.text for step in reversed(steps)
                   if step.role in ("writer", "verifier") and step.text.strip()), "")
    if not answer:
        answer = next((step.text for step in reversed(steps) if step.text.strip()), "")
    return OrchestrationResult(answer=answer, pattern="sequential", steps=steps, notes=notes)


def parallel(complete: Complete, goal: str, *,
             roles: Sequence[str] = ("researcher", "analyst"),
             synthesizer: str = "writer",
             history: Sequence[dict] | None = None,
             emit: Callable | None = None) -> OrchestrationResult:
    """Веер: независимые роли параллельно, затем синтез.

    Параллельность — через потоки: каждая роль делает блокирующий HTTP-вызов,
    поэтому потоки дают реальный выигрыш во времени. Ошибка одной ветки не
    отменяет остальные.
    """
    import threading

    steps: list[RoleResult] = []
    lock = threading.Lock()

    def run_role(role: str) -> None:
        result = _ask(complete, role, goal, history=history, emit=emit)
        with lock:
            steps.append(result)

    threads = [threading.Thread(target=run_role, args=(role,), daemon=True,
                                name=f"orchestrate-{role}") for role in roles]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    steps.sort(key=lambda item: list(roles).index(item.role) if item.role in roles else 99)
    notes = [f"параллельно: {', '.join(roles)} → синтез «{synthesizer}»"]
    materials = _context_of(steps)
    if not materials:
        notes.append("все ветки пусты")
        return OrchestrationResult(answer="", pattern="parallel", steps=steps, notes=notes)
    synthesis = _ask(complete, synthesizer, goal, context=materials, history=history,
                     emit=emit, max_tokens=1600)
    steps.append(synthesis)
    return OrchestrationResult(answer=synthesis.text, pattern="parallel",
                               steps=steps, notes=notes)


def critic(complete: Complete, goal: str, *, max_rounds: int = 2,
           drafter: str = "writer", critic_role: str = "critic",
           history: Sequence[dict] | None = None,
           emit: Callable | None = None) -> OrchestrationResult:
    """Черновик → критика → правка. Возвращает последний принятый вариант."""
    steps: list[RoleResult] = []
    notes: list[str] = []
    draft = _ask(complete, drafter, goal, history=history, emit=emit)
    steps.append(draft)
    if not draft.text.strip():
        return OrchestrationResult(answer="", pattern="critic", steps=steps,
                                   notes=["черновик пуст"])
    answer = draft.text
    verdict = ""
    for round_index in range(1, max(1, int(max_rounds)) + 1):
        review = _ask(complete, critic_role, goal,
                      context=f"[черновик]\n{answer}", history=history, emit=emit,
                      max_tokens=800)
        steps.append(review)
        verdict = review.text.strip()
        if review.ok and _OK.match(verdict):
            notes.append(f"принято с раунда {round_index}")
            break
        if not review.ok or not verdict:
            notes.append(f"раунд {round_index}: критик недоступен, оставляю черновик")
            break
        notes.append(f"раунд {round_index}: есть замечания — правлю")
        revised = _ask(complete, drafter, goal,
                       context=f"[черновик]\n{answer}\n\n[замечания критика]\n{verdict}",
                       history=history, emit=emit, max_tokens=1600)
        steps.append(revised)
        if not revised.text.strip():
            notes.append("правка не удалась — остаётся предыдущий вариант")
            break
        answer = revised.text
    return OrchestrationResult(answer=answer, pattern="critic", steps=steps,
                               notes=notes, verdict=verdict)


def router(complete: Complete, goal: str, *, chains: Sequence[Sequence[str]],
           history: Sequence[dict] | None = None,
           emit: Callable | None = None) -> OrchestrationResult:
    """Цепочка с деградацией: первый маршрут, который дал непустой ответ."""
    steps: list[RoleResult] = []
    notes: list[str] = []
    for index, chain in enumerate(chains, 1):
        notes.append(f"маршрут {index}: {', '.join(chain)}")
        context = ""
        route_steps: list[RoleResult] = []
        for role in chain:
            result = _ask(complete, role, goal, context=context, history=history, emit=emit)
            route_steps.append(result)
            if result.text.strip():
                context = _context_of(route_steps)
        steps.extend(route_steps)
        answer = next((item.text for item in reversed(route_steps) if item.text.strip()), "")
        if answer:
            notes.append(f"использован маршрут {index}")
            return OrchestrationResult(answer=answer, pattern="router", steps=steps, notes=notes)
    notes.append("все маршруты пусты")
    return OrchestrationResult(answer="", pattern="router", steps=steps, notes=notes)


def choose_pattern(goal: str) -> str:
    """Автовыбор паттерна под запрос (``AGENT_PATTERN=auto``)."""
    text = str(goal or "")
    if _REVIEW.search(text) or re.search(r"(?i)\bкод\b.*\b(?:ошиб|баг|исправ)", text):
        return "critic"
    if _COMPARE.search(text) or _MULTI.search(text) or len(text) > 320:
        return "parallel"
    if _RESEARCH.search(text):
        return "sequential"
    return "sequential"


def run(complete: Complete, goal: str, *, pattern: str = "auto",
        history: Sequence[dict] | None = None, emit: Callable | None = None,
        max_rounds: int = 2) -> OrchestrationResult:
    """Единая точка входа: выбирает паттерн и выполняет его.

    ``off`` возвращает пустой результат — вызывающий код остаётся на прежнем
    пути (один исполнитель).
    """
    chosen = str(pattern or "auto").strip().lower()
    if chosen not in PATTERNS:
        chosen = "auto"
    if chosen == "off":
        return OrchestrationResult(answer="", pattern="off",
                                   notes=["оркестрация выключена"])
    resolved = choose_pattern(goal) if chosen == "auto" else chosen
    if emit:
        emit({"type": "stage", "scene": "write",
              "title": f"Мультиагентный режим: {resolved}",
              "text": "Роли: " + ", ".join(_roles_for(resolved))})
    started = time.monotonic()
    if resolved == "critic":
        result = critic(complete, goal, max_rounds=max_rounds, history=history, emit=emit)
    elif resolved == "parallel":
        result = parallel(complete, goal, history=history, emit=emit)
    elif resolved == "router":
        result = router(complete, goal, history=history,
                        chains=[("researcher", "writer"), ("writer",)], emit=emit)
    else:
        result = sequential(complete, goal, history=history, emit=emit)
    result.notes.append(f"паттерн {resolved} · {time.monotonic() - started:.1f} c")
    return result


def _roles_for(pattern: str) -> list[str]:
    return {"sequential": ["planner", "writer"], "parallel": ["researcher", "analyst", "writer"],
            "critic": ["writer", "critic"], "router": ["researcher", "writer"]}.get(
        pattern, ["writer"])


def summary() -> dict:
    """Состояние для диагностики."""
    import os
    return {"pattern": (os.getenv("AGENT_PATTERN") or "off").strip().lower(),
            "patterns": list(PATTERNS), "roles": list(ROLES)}
