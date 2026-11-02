"""Evidence-based completion checks for action-oriented requests."""
from __future__ import annotations

import re

_ACTION = re.compile(r"(?iu)\b(?:создай|сделай|напиши|сохрани|запусти|выполни|проверь|исправь|установи|создать|написать|запустить|проверить|build|create|write|save|run|execute|test|deploy)\b")
_WRITE = re.compile(r"(?iu)(?:файл|отч[её]т|сайт|приложени|код|документ|запиши|сохрани|напиши|create|write|website|report|file)")
_CHECK = re.compile(r"(?iu)(?:проверь|протестируй|тест|запусти|выполни|проверить|test|run|execute)")
_CLAIM = re.compile(r"(?iu)(?:готово|выполнено|создал(?:а)?|сделал(?:а)?|запустил(?:а)?|проверил(?:а)?|успешно|готовый результат|done|completed|created|successfully)")


def verify_task(goal: str, observations: list[dict]) -> dict:
    """Return evidence required for claims about concrete work.

    A plain informational answer does not require tool evidence. Action requests
    do; file creation and verification requests each require corresponding
    successful tools in the observation log.
    """
    goal = str(goal or "")
    needs_action = bool(_ACTION.search(goal))
    needs_write = needs_action and bool(_WRITE.search(goal))
    needs_check = needs_action and bool(_CHECK.search(goal))
    successful = []
    for item in observations or []:
        if not isinstance(item, dict):
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if result.get("ok") is True:
            successful.append({"tool": str(item.get("tool") or ""), "result": result})
    write_tools = {"file_write", "file_edit", "file_create"}
    check_tools = {"shell", "python", "terminal"}
    has_write = any(item["tool"] in write_tools for item in successful)
    has_check = any(item["tool"] in check_tools for item in successful)
    missing = []
    if needs_action and not successful:
        missing.append("успешный результат инструмента")
    if needs_write and not has_write:
        missing.append("подтверждение создания или изменения файла")
    if needs_check and not has_check:
        missing.append("результат проверки/команды")
    return {"verified": needs_action and not missing if needs_action else True,
            "required": needs_action, "missing": missing,
            "evidence": [{"tool": item["tool"],
                          "summary": str(item["result"].get("result") or item["result"].get("content") or item["result"].get("output") or "успешный ответ")[:300]}
                         for item in successful[:12]]}


def honest_answer(goal: str, answer: str, verification: dict) -> str:
    """Prevent unsupported completion claims without masking non-success."""
    text = str(answer or "").strip()
    if verification.get("required") and not verification.get("verified") and _CLAIM.search(text):
        missing = ", ".join(verification.get("missing") or ["доказательства"])
        return f"Не могу честно подтвердить завершение задачи: нет {missing}.\n\n{text}"
    return text
