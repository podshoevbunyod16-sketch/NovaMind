"""Набор задач агента (evals): разбор YAML, проверка ответа и отчёт."""
import json
import os

import pytest

from evals import runner, yamlmini

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS_DIR = os.path.join(ROOT, "evals", "tasks")


# ══════════════ мини-YAML ══════════════

def test_parse_flat_document():
    documents = yamlmini.parse_documents("id: a-1\nmax_steps: 5\nok: true\n")
    assert documents == [{"id": "a-1", "max_steps": 5, "ok": True}]


def test_parse_multiple_documents_and_comments():
    text = """# комментарий
id: first
prompt: привет  # хвостовой комментарий
---
id: second
prompt: "строка с # внутри"
"""
    documents = yamlmini.parse_documents(text)
    assert [doc["id"] for doc in documents] == ["first", "second"]
    assert documents[0]["prompt"] == "привет"
    assert documents[1]["prompt"] == "строка с # внутри"


def test_parse_inline_list():
    documents = yamlmini.parse_documents("tools: [search, open]\n")
    assert documents[0]["tools"] == ["search", "open"]


def test_parse_rejects_broken_line():
    with pytest.raises(yamlmini.YamlError):
        yamlmini.parse_documents("просто текст без двоеточия\n")


# ══════════════ набор задач ══════════════

def test_task_set_has_25_unique_tasks():
    tasks = yamlmini.load_tasks(TASKS_DIR)
    assert len(tasks) == 25
    assert len({task["id"] for task in tasks}) == 25
    assert all(task["prompt"] for task in tasks)


def test_task_set_covers_all_categories():
    tasks = yamlmini.load_tasks(TASKS_DIR)
    categories = {task.get("category") for task in tasks}
    assert categories == {"search", "terminal", "code", "memory", "safety"}


def test_task_without_prompt_is_rejected(tmp_path):
    (tmp_path / "bad.yaml").write_text("id: no-prompt\nmax_steps: 3\n", encoding="utf-8")
    with pytest.raises(yamlmini.YamlError):
        yamlmini.load_tasks(str(tmp_path))


# ══════════════ разбор потока ══════════════

def test_parse_events_skips_broken_lines():
    events = runner.parse_events(['{"type": "token", "token": "при"}', "", "не json", '{"type": "done"}'])
    assert [event["type"] for event in events] == ["token", "done"]


def test_summarize_collects_answer_steps_and_tools():
    events = [
        {"type": "step", "icon": "🔎", "text": "ищу"},
        {"type": "tool", "name": "search"},
        {"type": "token", "token": "Привет"},
        {"type": "token", "token": ", мир"},
        {"type": "result", "reply": "Привет, мир", "usage": {"total_tokens": 42}},
    ]
    summary = runner.summarize(events)
    assert summary["answer"] == "Привет, мир"
    assert summary["steps"] == 1 and summary["tools"] == ["search"]
    assert summary["tokens"] == 42


def test_tokens_are_estimated_without_usage():
    summary = runner.summarize([{"type": "result", "reply": "двадцать символов!"}])
    assert 1 <= summary["tokens"] <= 10


# ══════════════ оценка задачи ══════════════

BASE_TASK = {"id": "t", "expect": ["биткоин"], "tools": ["search"],
             "forbid_tools": [], "max_steps": 5}


def test_evaluate_success():
    result = {"answer": "Курс биткоина вырос", "steps": 2, "tools": ["search"]}
    assert runner.evaluate(BASE_TASK, result) == (True, "")


def test_evaluate_requires_expected_text():
    ok, reason = runner.evaluate(BASE_TASK, {"answer": "не знаю", "steps": 1, "tools": ["search"]})
    assert ok is False and "биткоин" in reason


def test_evaluate_requires_tool():
    ok, reason = runner.evaluate(BASE_TASK, {"answer": "биткоин", "steps": 1, "tools": []})
    assert ok is False and "search" in reason


def test_evaluate_rejects_forbidden_tool():
    task = dict(BASE_TASK, forbid_tools=["run"])
    ok, reason = runner.evaluate(task, {"answer": "биткоин", "steps": 1, "tools": ["search", "run"]})
    assert ok is False and "run" in reason


def test_evaluate_rejects_step_overflow_and_errors():
    task = dict(BASE_TASK, tools=[])
    assert runner.evaluate(task, {"answer": "биткоин", "steps": 99})[0] is False
    assert runner.evaluate(task, {"answer": "", "steps": 1})[0] is False
    assert runner.evaluate(task, {"answer": "биткоин", "steps": 1, "error": "сбой"})[0] is False


# ══════════════ прогон и отчёт ══════════════

def _transport(prompt):
    """Фейковый сервер: отвечает потоком, как настоящий агент."""
    events = [
        {"type": "step", "icon": "🔎", "text": "ищу"},
        {"type": "tool", "name": "search"},
        {"type": "token", "token": "Курс биткоина — 100"},
        {"type": "result", "reply": "Курс биткоина — 100"},
    ]
    return events, 0.01


def test_run_task_success():
    task = {"id": "demo", "category": "search", "prompt": "курс", "expect": ["биткоин"],
            "tools": ["search"], "forbid_tools": [], "max_steps": 5}
    result = runner.run_task(task, _transport)
    assert result["ok"] is True and result["steps"] == 1
    assert result["time"] >= 0 and result["tokens"] > 0


def test_run_task_survives_transport_failure():
    def broken(prompt):
        raise OSError("сервер недоступен")
    task = {"id": "demo", "prompt": "курс", "expect": [], "tools": [], "forbid_tools": [], "max_steps": 5}
    result = runner.run_task(task, broken)
    assert result["ok"] is False and "транспорт" in result["reason"]


def test_format_table_and_summary():
    results = [
        {"id": "a", "ok": True, "steps": 2, "time": 1.5, "tokens": 10, "reason": ""},
        {"id": "b", "ok": False, "steps": 4, "time": 2.5, "tokens": 20, "reason": "нет ответа"},
    ]
    table = runner.format_table(results)
    assert "Задача" in table and "✅" in table and "❌" in table
    line = runner.summary_line(results)
    assert "1/2" in line and "токенов 30" in line


def test_main_reports_missing_arguments(capsys):
    assert runner.main(["--unknown"]) == 2
    assert "Неизвестный аргумент" in capsys.readouterr().out


def test_main_runs_with_fake_transport(monkeypatch, tmp_path, capsys):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "one.yaml").write_text(
        "id: demo\ncategory: search\nprompt: курс биткоина\ntools: search\nexpect: биткоин\nmax_steps: 3\n",
        encoding="utf-8")
    monkeypatch.setattr(runner, "HttpTransport", lambda *a, **k: _transport)
    code = runner.main(["--tasks", str(tasks_dir), "--json", str(tmp_path / "report.json")])
    output = capsys.readouterr().out
    assert code == 0 and "Успех: 1/1" in output
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report[0]["id"] == "demo" and report[0]["ok"] is True
