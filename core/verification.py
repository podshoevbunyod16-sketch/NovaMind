"""Evidence-based verification for autonomous task outcomes."""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict


@dataclass
class VerificationResult:
    status: str
    ok: bool
    summary: str
    evidence: list[dict]
    unresolved: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


def verify(goal: str, answer: str, state, cancelled: bool = False) -> VerificationResult:
    """Evaluate completion from execution evidence, never response length alone."""
    snapshot = state.snapshot() if hasattr(state, "snapshot") else dict(state or {})
    stats = snapshot.get("tool_stats") or {}
    successes = stats.get("ok") or {}
    failures = stats.get("failed") or {}
    observations = list(snapshot.get("observations") or [])
    sources = list(snapshot.get("sources") or [])
    evidence = [item for item in observations if item.get("ok")]
    unresolved = []
    low = str(goal or "").lower()
    answer_ok = bool(str(answer or "").strip())

    if cancelled:
        return VerificationResult("cancelled", False, "Задача отменена пользователем", evidence, [])
    if snapshot.get("status") == "error":
        return VerificationResult("failed", False, snapshot.get("error") or "Выполнение завершилось ошибкой", evidence,
                                  [snapshot.get("error") or "runtime error"])

    operation = bool(re.search(r"созда|измени|исправ|удали|запусти|подключ|отправ|commit|push|fix|write|create", low))
    coding = bool(re.search(r"код|репозитор|repository|bug|ошиб|исправ|test|тест|fix", low))
    research = bool(re.search(r"найди|поиск|research|источник|актуаль|новост", low))
    tests_seen = any(item.get("ok") and re.search(
        r"pytest|npm test|unittest|test(s)?\b|py_compile|check",
        str(item.get("command") or item.get("summary") or ""), re.I)
        for item in observations)
    if operation and not successes:
        unresolved.append("Нет успешного инструментального подтверждения запрошенной операции")
    if coding and operation and successes and not tests_seen:
        unresolved.append("Изменения не подтверждены запуском подходящей проверки")
    if research and not (sources or evidence):
        unresolved.append("Нет полученных источников или результатов исследования")
    if failures:
        failed_total = sum(int(v or 0) for v in failures.values())
        successful_total = sum(int(v or 0) for v in successes.values())
        if failed_total and not successful_total:
            unresolved.append("Все инструментальные попытки завершились ошибкой")
    if not answer_ok:
        unresolved.append("Нет итогового ответа")

    if unresolved:
        needs_user = any(re.search(r"пользователь|разрешени|permission|credential|token|401|403",
                                   str(item.get("summary") or ""), re.I)
                         for item in observations if not item.get("ok"))
        status = ("requires_user_action" if needs_user else
                  "partially_completed" if (answer_ok and (evidence or successes or sources)) else "failed")
        return VerificationResult(status, False, "; ".join(unresolved), evidence[-8:], unresolved)
    return VerificationResult("completed", True, "Цель подтверждена результатами выполнения", evidence[-8:], [])
