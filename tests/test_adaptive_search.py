"""Поиск по раундам: агент сам решает, хватает ли данных, и продолжает искать."""
import json

import pytest

import agenticseek.engine as engine_mod
from agenticseek.engine import AgentEngine, EngineContext
from agenticseek.tools.terminal_commands import _delegated_input
from agenticseek.tools.web_tools import WebSearchTool


def _items(prefix, count):
    return [{"title": f"{prefix} {i}", "url": f"https://example.com/{prefix}/{i}",
             "snippet": f"сниппет {prefix} {i}"} for i in range(count)]


def _engine(search, complete):
    events = []
    ctx = EngineContext(goal="5 фактов про Исмаила Сомони", complete=complete,
                        emit=events.append, search=search,
                        read_page=lambda url, *a, **k: (f"текст {url}", None),
                        force_search=True)
    return AgentEngine(ctx), events


@pytest.fixture(autouse=True)
def rounds(monkeypatch):
    monkeypatch.setattr(engine_mod, "SEARCH_ROUNDS", 4)


def test_stops_after_first_round_when_data_is_enough():
    queries = []

    def search(query, limit):
        queries.append((query, limit))
        return _items("a", 10), {}

    engine, _ = _engine(search, lambda *a, **k: ('{"enough": true}', {}))
    text = engine._run_forced_search()

    assert len(queries) == 1 and queries[0][1] == engine_mod.SEARCH_LIMIT == 10
    assert "данных достаточно" in text


def test_keeps_searching_with_a_new_query_until_enough():
    queries, verdicts = [], iter([
        '{"enough": false, "query": "Исмаил Сомони биография даты"}',
        '{"enough": true}',
    ])

    def search(query, limit):
        queries.append(query)
        # второй раунд возвращает один повтор и два новых источника
        return (_items("a", 3) if len(queries) == 1 else _items("a", 1) + _items("b", 2)), {}

    engine, events = _engine(search, lambda *a, **k: (next(verdicts), {}))
    text = engine._run_forced_search()

    assert queries[1] == "Исмаил Сомони биография даты"
    assert len(engine._found) == 5                     # повтор не задвоен
    assert "[4]" in text and "[5]" in text             # нумерация сквозная
    assert any("уточняю запрос" in e.get("text", "") for e in events if e["type"] == "step")
    assert "данных достаточно" in text


def test_round_limit_stops_endless_search(monkeypatch):
    monkeypatch.setattr(engine_mod, "SEARCH_ROUNDS", 3)
    calls = []

    def search(query, limit):
        calls.append(query)
        return _items(f"r{len(calls)}", 2), {}

    def complete(*a, **k):
        return json.dumps({"enough": False, "query": f"запрос {len(calls)}"}), {}

    engine, _ = _engine(search, complete)
    text = engine._run_forced_search()

    assert len(calls) == 3
    assert "поиск завершён" in text and "чего именно не хватило" in text


def test_judge_failure_answers_with_what_was_found():
    calls = []

    def search(query, limit):
        calls.append(query)
        return _items("a", 4), {}

    def broken(*a, **k):
        raise RuntimeError("provider down")

    engine, _ = _engine(search, broken)
    text = engine._run_forced_search()

    assert len(calls) == 1 and "Результаты поиска" in text


def test_first_search_empty_keeps_old_behaviour():
    engine, _ = _engine(lambda q, l: ([], {}), lambda *a, **k: ('{"enough": true}', {}))
    assert "ничего не нашёл" in engine._run_forced_search()


def test_web_search_tool_honours_limit_from_json_payload():
    seen = []
    tool = WebSearchTool(lambda q, n: (seen.append(n) or _items("a", 20), {}))

    payload, _ = _delegated_input("web_search", json.dumps({"query": "asyncio", "limit": 15}))
    output = tool.execute(payload)

    assert seen == [15] and output.count("https://example.com/") == 15
    tool.execute("asyncio")                              # без limit — как раньше
    assert seen[-1] == tool.limit
