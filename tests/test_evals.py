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

def test_task_set_has_unique_tasks_with_prompts():
    # Набор растёт вместе с возможностями агента (сейчас 35 задач), поэтому
    # проверяем инварианты, а не точное число: уникальные id и у каждой задачи
    # есть запрос.
    tasks = yamlmini.load_tasks(TASKS_DIR)
    assert len(tasks) >= 25
    assert len({task["id"] for task in tasks}) == len(tasks)
    assert all(task["prompt"] for task in tasks)


def test_task_set_covers_all_categories():
    tasks = yamlmini.load_tasks(TASKS_DIR)
    categories = {task.get("category") for task in tasks}
    # Базовые категории + новые подсистемы: знания (RAG), ограждения,
    # мультиагентная оркестрация.
    assert {"search", "terminal", "code", "memory", "safety"} <= categories
    assert {"knowledge", "guardrails", "orchestration"} <= categories


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


# ══════════════ офлайн-прогон и лимит запросов ══════════════

def test_summarize_marks_offline_provider():
    events = [{"type": "token", "token": "привет"},
              {"type": "result", "reply": "привет", "provider": "local_demo"}]
    summary = runner.summarize(events)
    assert summary["offline"] is True and summary["provider"] == "local_demo"


def test_summarize_marks_online_provider():
    events = [{"type": "result", "reply": "ок", "provider": "groq", "offline": False}]
    summary = runner.summarize(events)
    assert summary["offline"] is False and summary["provider"] == "groq"


def test_is_rate_limited_detects_message():
    assert runner.is_rate_limited([{"type": "error", "text": "Слишком много запросов к агенту."}])
    assert runner.is_rate_limited([{"type": "error", "text": "Too many requests"}])
    assert not runner.is_rate_limited([{"type": "error", "text": "провайдер недоступен"}])
    assert not runner.is_rate_limited([])


def test_run_task_reports_offline_flag():
    def offline_transport(prompt):
        return ([{"type": "token", "token": "офлайн"},
                 {"type": "result", "reply": "офлайн", "provider": "local_demo"}], 0.01)
    task = {"id": "demo", "category": "chat", "prompt": "привет", "expect": ["офлайн"],
            "tools": [], "forbid_tools": [], "max_steps": 5}
    result = runner.run_task(task, offline_transport)
    assert result["offline"] is True and result["provider"] == "local_demo"


class _FakeResponse:
    def __init__(self, status_code, payload=None, retry_after=None):
        import json as _json
        self.status_code = status_code
        self._payload = payload or []
        self.headers = {}
        if retry_after is not None:
            self.headers["Retry-After"] = str(retry_after)
        self.text = _json.dumps({"error": "Слишком много запросов к агенту."})

    def json(self):
        return {"error": "Слишком много запросов к агенту."}

    def iter_lines(self, decode_unicode=True):
        import json as _json
        for event in self._payload:
            yield _json.dumps(event, ensure_ascii=False)


def test_transport_retries_short_rate_limit(monkeypatch):
    import sys
    import types
    calls = []

    def fake_post(url, json=None, headers=None, stream=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            return _FakeResponse(429, retry_after=0)      # Retry-After: 0 → без ожидания
        return _FakeResponse(200, [{"type": "result", "reply": "готово"}])

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=fake_post))
    transport = runner.HttpTransport("http://example.invalid", retries=1)
    events, _elapsed = transport("привет")
    assert len(calls) == 2
    assert events == [{"type": "result", "reply": "готово"}]


def test_transport_does_not_wait_out_long_rate_limit(monkeypatch):
    import sys
    import types
    calls = []

    def fake_post(url, json=None, headers=None, stream=None, timeout=None):
        calls.append(url)
        return _FakeResponse(429, retry_after=3600)       # окно в час — ждать бессмысленно

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=fake_post))
    transport = runner.HttpTransport("http://example.invalid", retries=3)
    events, _elapsed = transport("привет")
    assert len(calls) == 1
    assert runner.is_rate_limited(events)


def test_run_all_stops_on_rate_limit():
    calls = []

    def transport(prompt):
        calls.append(prompt)
        return [{"type": "error", "text": "Слишком много запросов к агенту."}], 0.01

    tasks = [{"id": f"t{i}", "prompt": "p", "expect": [], "tools": [],
              "forbid_tools": [], "max_steps": 3} for i in range(5)]
    results = runner.run_all(tasks, transport)
    assert len(results) == 1 and results[0]["rate_limited"] is True
    assert calls == ["p"]


def test_main_stops_and_hints_on_rate_limit(monkeypatch, tmp_path, capsys):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "one.yaml").write_text(
        "id: demo\ncategory: search\nprompt: курс\nmax_steps: 3\n", encoding="utf-8")

    def limited(prompt):
        return [{"type": "error", "text": "Слишком много запросов к агенту."}], 0.01

    monkeypatch.setattr(runner, "HttpTransport", lambda *a, **k: limited)
    report = tmp_path / "report.json"
    code = runner.main(["--tasks", str(tasks_dir), "--json", str(report)])
    output = capsys.readouterr().out
    assert code == 3
    assert "AGENT_RATE_LIMIT=0" in output
    assert not report.exists()
