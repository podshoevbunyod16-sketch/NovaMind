"""Completion verification requires observations, not a planning-role claim."""
from agent.verification import honest_answer, verify_task


def _observation(tool, result):
    return {"tool": tool, "arguments": {}, "result": result}


def test_informational_answer_does_not_require_an_action_tool():
    verdict = verify_task("Что такое SQLite?", [])
    assert verdict["verified"] is True
    assert verdict["required"] is False
    assert verdict["missing"] == []


def test_create_request_needs_successful_file_evidence():
    verdict = verify_task("Создай файл отчёта", [])
    assert verdict["verified"] is False
    assert "подтверждение создания или изменения файла" in verdict["missing"]
    reply = honest_answer("Создай файл отчёта", "Готово, отчёт создан.", verdict)
    assert "Не могу честно подтвердить завершение" in reply


def test_failed_tool_result_is_not_completion_evidence():
    verdict = verify_task("Создай файл отчёта", [
        _observation("file_write", {"ok": False, "error": "permission denied"}),
    ])
    assert verdict["verified"] is False
    assert verdict["evidence"] == []


def test_successful_file_and_check_tools_verify_matching_requests():
    observations = [
        _observation("file_write", {"ok": True, "path": "report.md", "result": "created"}),
        _observation("shell", {"ok": True, "result": "pytest: 3 passed"}),
    ]
    write_verdict = verify_task("Создай файл отчёта", observations)
    check_verdict = verify_task("Запусти тесты и проверь проект", observations)
    assert write_verdict["verified"] is True
    assert check_verdict["verified"] is True
    assert {item["tool"] for item in write_verdict["evidence"]} == {"file_write", "shell"}


def test_non_completion_language_does_not_get_rewritten_as_success():
    answer = "Не удалось создать файл: доступ к рабочей папке выключен."
    verdict = verify_task("Создай файл отчёта", [])
    assert honest_answer("Создай файл отчёта", answer, verdict) == answer
