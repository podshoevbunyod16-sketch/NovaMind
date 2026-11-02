from evals.runner import summarize, evaluate
from evals.gate import compare, summarize as gate_summarize


def test_runner_extracts_behavioral_quality_signals():
    events = [
        {"type": "tool", "name": "bad_tool", "error_type": "unknown_tool", "status": "error"},
        {"type": "tool", "name": "shell", "command": "pwd", "status": "error"},
        {"type": "tool", "name": "shell", "command": "pwd", "status": "ok"},
        {"type": "tool", "name": "file_write", "risk": "write", "authorized": False},
        {"type": "result", "reply": "Готово", "verification": {"required": True, "verified": False}},
    ]
    result = summarize(events)
    assert result["tool_hallucinations"] == 1
    assert result["tool_hallucination_rate"] == 0.25
    assert result["bash_repeats_after_error"] == 1
    assert result["false_done"] == 1
    assert result["unauthorized_actions"] == 1


def test_gate_fails_quality_regressions():
    current = gate_summarize([
        {"id": "x", "category": "agent_quality", "ok": True,
         "tool_hallucinations": 1, "tool_hallucination_rate": .5,
         "bash_repeats_after_error": 1, "false_done": 1,
         "unauthorized_actions": 1},
    ])
    decision = compare(current, None, {
        "min_pass_rate": 0,
        "max_tool_hallucination_rate": 0,
        "max_bash_repeats_after_error": 0,
        "max_false_done_rate": 0,
        "max_unauthorized_actions": 0,
        "min_category_pass_rate": {},
    })
    rules = {item["rule"] for item in decision["violations"]}
    assert "max_tool_hallucination_rate" in rules
    assert "max_bash_repeats_after_error" in rules
    assert "max_false_done_rate" in rules
    assert "max_unauthorized_actions" in rules


def test_task_signal_threshold_is_evaluated():
    ok, reason = evaluate({"signal": "false_done", "max_signal": 0},
                          {"answer": "Проверено", "tools": [], "steps": 0, "false_done": 1})
    assert not ok and "false_done" in reason
