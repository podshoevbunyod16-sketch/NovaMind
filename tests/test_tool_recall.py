"""Динамическое получение инструментов и размышление вокруг каждого шага.

Пользователь видит такой порядок хода:

1. «Понимаю запрос» (первый шаг);
2. «Вспоминаю инструменты…» / «Вспоминаю MCP-серверы…» — живая анимация;
3. инструменты реально собираются под запрос и строка становится «Вспомнил …»;
4. перед каждым шагом «Думает…», после шага — «Думал» (осмыслил результат).

Когда автопоиск выключен, инструментов нет вообще: показывается честная
строка «Инструменты не нужны», а запрос уходит прямо в ИИ-модель.
"""

import json

import routes.agent as agent_routes
from agenticseek import timeline
from tests.agent_helpers import events_of, ndjson, script


# ══════════════ события таймлайна ══════════════

def test_recall_event_shape():
    live = timeline.recall_event("tools", "Вспоминаю инструменты",
                                 detail="терминал, поиск", items=["terminal", "web_search"])
    assert live["type"] == "recall" and live["phase"] == "tools"
    assert live["done"] is False and live["text"] == "Вспоминаю инструменты"
    assert live["items"] == ["terminal", "web_search"]

    done = timeline.recall_event("tools", "Вспомнил инструменты", done=True,
                                 icon="✅", items=["terminal"])
    assert done["done"] is True and done["icon"] == "✅"
    assert "items" not in timeline.recall_event("mcp", "Вспоминаю MCP-серверы")


def test_think_event_shape():
    before = timeline.think_event("before", "pwd")
    after = timeline.think_event("after", "pwd", done=True)
    assert before["type"] == "think" and before["phase"] == "before"
    assert before["done"] is False and "pwd" in before["text"]
    assert after["phase"] == "after" and after["done"] is True
    assert after["label"] == "Думал"


# ══════════════ порядок: понять → вспомнить → получить ══════════════

def test_understanding_comes_before_tool_recall(admin_client, sandbox, monkeypatch):
    """Первый шаг — понимание запроса, и только потом «вспоминаю инструменты»."""
    script(monkeypatch, ["42"])
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "сколько будет 6*7"}))

    assert events[0]["type"] == "stage", events[:3]
    assert "Понимаю" in events[0]["title"]
    recalls = events_of(events, "recall")
    live = [event for event in recalls if not event["done"]]
    assert any("Вспоминаю инструменты" in event["text"] for event in live), recalls
    kinds = [event["type"] for event in events]
    assert kinds.index("stage") < kinds.index("recall")

    ready = next(event for event in recalls if event["phase"] == "ready")
    assert ready["done"] is True and ready.get("items"), ready
    assert "terminal" in ready["items"], "инструменты реально подключены"


def test_tools_are_loaded_only_after_recall_started(admin_client, sandbox, monkeypatch):
    """Живая строка «вспоминаю» приходит раньше сообщения о полученных тегах."""
    script(monkeypatch, ["готово"])
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "создай файл hello.txt"}))
    start = next(index for index, event in enumerate(events)
                 if event["type"] == "recall" and event["phase"] == "tools"
                 and not event["done"])
    ready = next(index for index, event in enumerate(events)
                 if event["type"] == "recall" and event["phase"] == "ready")
    loaded = next(index for index, event in enumerate(events)
                  if event["type"] == "step" and "Получил инструменты" in event.get("text", ""))
    assert start < ready < loaded
    assert any(event["phase"] == "tools" and event["done"]
               for event in events_of(events, "recall"))


def test_mcp_servers_are_recalled_for_mcp_request(admin_client, sandbox, monkeypatch):
    """Просьба про MCP/GitHub показывает отдельную анимацию «вспоминаю MCP-серверы»."""
    script(monkeypatch, ["Нашёл репозитории."])
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "используй MCP-серверы github и найди репозитории по flask"}))
    texts = [event.get("text", "") for event in events_of(events, "recall")]
    assert any("Вспоминаю MCP-серверы" in text for text in texts), texts
    assert any("Вспомнил MCP-серверы" in text for text in texts), texts


def test_plain_question_does_not_recall_mcp_servers(admin_client, sandbox, monkeypatch):
    """Обычный вопрос не засорятся строками про MCP."""
    script(monkeypatch, ["42"])
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "сколько будет 6*7"}))
    texts = [event.get("text", "") for event in events_of(events, "recall")]
    assert not any("MCP-серверы" in text for text in texts), texts


# ══════════════ автопоиск выключен ══════════════

def test_no_tools_mode_reports_that_no_tools_are_needed(admin_client, sandbox, monkeypatch):
    """tools:false — ни «вспоминаю инструменты», ни шагов, ни MCP."""
    script(monkeypatch, ["Ответ без инструментов."])
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "выполни pwd и найди погоду", "tools": False, "search": False}))
    recalls = events_of(events, "recall")
    assert recalls and all(event["phase"] == "plain" for event in recalls), recalls
    assert not any("Вспоминаю инструменты" in event.get("text", "") for event in recalls)
    assert not events_of(events, "think"), "без инструментов нет шагов"
    assert not events_of(events, "tool")
    result = next(event for event in events if event["type"] == "result")
    assert result["steps"] == 0 and result["tools"] == []


# ══════════════ думаем перед каждым шагом и после него ══════════════

def test_each_terminal_step_is_surrounded_by_thinking(admin_client, sandbox, monkeypatch):
    """Команда терминала: «Думает…» → шаг → «Думал» (осмыслил результат)."""
    script(monkeypatch, ["```terminal\npwd\n```", "Готово: команда выполнена."])
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "выполни pwd в терминале"}))
    tools = events_of(events, "tool")
    assert tools and tools[0]["name"] == "terminal", tools
    kinds = [event["type"] for event in events
             if event["type"] in ("think", "tool")]
    assert kinds[:3] == ["think", "tool", "think"], kinds[:6]
    thinks = events_of(events, "think")
    assert thinks[0]["phase"] == "before" and thinks[0]["done"] is False
    assert thinks[1]["phase"] == "after" and thinks[1]["done"] is True


def test_thinking_also_wraps_offline_terminal_steps(admin_client, sandbox, monkeypatch):
    """Демо-режим без ключа: офлайн-терминал думает вокруг каждой команды."""
    monkeypatch.setattr(agent_routes, "resolve_target",
                        lambda *a, **k: ("local_demo", "nova-local-1", {}))
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "выполни pwd и uname в терминале"}))
    tools = events_of(events, "tool")
    assert len(tools) >= 2 and all(tool["name"] == "terminal" for tool in tools)
    before = [event for event in events_of(events, "think") if event["phase"] == "before"]
    after = [event for event in events_of(events, "think") if event["phase"] == "after"]
    assert len(before) >= 2 and len(after) >= 2
    # После каждой команды агент думает снова — перед тем, как выбрать следующую.
    assert after[-1]["done"] is True


# ══════════════ трасса шагов сохраняет новые события ══════════════

def test_trace_entries_keep_finished_recall_and_thinking():
    def entries(event):
        return agent_routes._trace_entries(json.dumps(event, ensure_ascii=False))

    assert entries({"type": "recall", "phase": "tools", "done": False,
                    "text": "Вспоминаю инструменты"}) == [], "анимация не в трассе"
    assert entries({"type": "recall", "phase": "ready", "done": True,
                    "text": "Инструменты наготове"}) == [], "дубль ready не нужен"
    done_recall = entries({"type": "recall", "phase": "mcp", "done": True,
                           "icon": "🔌", "text": "Вспомнил MCP-серверы"})
    assert len(done_recall) == 1 and done_recall[0]["kind"] == "step"
    assert "MCP-серверы" in done_recall[0]["text"]
    done_think = entries({"type": "think", "phase": "after", "done": True,
                          "label": "Думал", "text": "Осмыслил результат"})
    assert len(done_think) == 1 and done_think[0]["kind"] == "thought"
    assert entries({"type": "think", "phase": "before", "done": False,
                    "text": "Думаю"}) == []
