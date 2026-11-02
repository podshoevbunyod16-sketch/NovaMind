"""Multi-round search is ordinary model-directed ReAct, not a specialist judge."""
import json

from agent.loop import AgentContext, run_react
from tool_registry import ToolSpec


def _items(prefix, count):
    return [{"title": f"{prefix} {index}",
             "url": f"https://example.test/{prefix}/{index}",
             "snippet": f"выдержка {prefix} {index}"}
            for index in range(count)]


def _search_spec(search):
    return ToolSpec("web_search", "Search fixture pages.", {
        "type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"], "additionalProperties": False,
    }, search, permissions=("network",), risk="safe")


def _call(query, limit=10):
    return f'```json\n{{"tool":"web_search","arguments":{{"query":{json.dumps(query, ensure_ascii=False)},"limit":{limit}}}}}\n```'


def _run(goal, search, complete, max_steps=12):
    spec = _search_spec(search)
    return run_react(AgentContext(
        goal, tools=[spec], complete=complete,
        call_tool=lambda name, **kwargs: spec.call(**kwargs), max_steps=max_steps,
    ))


def test_model_stops_after_one_search_when_it_has_enough_data():
    searches = []
    replies = iter([
        (_call("Исмаил Сомони факты"), {}),
        ("Нашёл достаточно фактов для ответа.", {}),
    ])
    result = _run(
        "Найди пять фактов об Исмаиле Сомони",
        lambda query, limit=10: searches.append((query, limit)) or
        {"ok": True, "results": _items("bio", 10)},
        lambda *a, **k: next(replies),
    )
    assert searches == [("Исмаил Сомони факты", 10)]
    assert result.steps == 1
    assert "достаточно" in result.answer


def test_model_can_refine_search_after_observing_first_results():
    searches = []
    replies = iter([
        (_call("Исмаил Сомони"), {}),
        (_call("Исмаил Сомони биография даты правления"), {}),
        ("Сводка опирается на оба поиска.", {}),
    ])

    def search(query, limit=10):
        searches.append(query)
        if len(searches) == 1:
            return {"ok": True, "results": _items("first", 3)}
        return {"ok": True, "results": _items("second", 2)}

    seen = []
    result = _run(
        "Найди пять фактов об Исмаиле Сомони", search,
        lambda messages, **kwargs: seen.append(str(messages)) or next(replies),
    )
    assert searches == ["Исмаил Сомони", "Исмаил Сомони биография даты правления"]
    assert result.steps == 2
    assert "first 0" in seen[1]
    assert "Сводка" in result.answer


def test_max_step_budget_bounds_model_directed_search():
    searches = []
    replies = iter([(_call(f"запрос {index}"), {}) for index in range(5)])
    result = _run(
        "Исследуй тему",
        lambda query, limit=10: searches.append(query) or
        {"ok": True, "results": _items(query, 2)},
        lambda *a, **k: next(replies), max_steps=3,
    )
    assert searches == ["запрос 0", "запрос 1", "запрос 2"]
    assert result.status == "budget_exhausted"
    assert result.steps == 3
    assert "лимит шагов" in result.answer.lower()
    assert len(result.observations) == 3


def test_provider_failure_preserves_search_observations_for_honest_fallback():
    searches = []
    replies = iter([(_call("Исмаил Сомони"), {}), (None, "provider unavailable")])
    result = _run(
        "Найди информацию",
        lambda query, limit=10: searches.append(query) or
        {"ok": True, "results": _items("found", 4)},
        lambda *a, **k: next(replies),
    )
    assert searches == ["Исмаил Сомони"]
    assert result.status == "provider_error"
    assert len(result.observations[0]["result"]["results"]) == 4
    assert "Не удалось получить ответ модели" in result.answer


def test_empty_search_result_is_visible_before_the_model_answers():
    seen = []
    replies = iter([(_call("ничего нет"), {}), ("Ничего не нашёл по этому запросу.", {})])
    result = _run(
        "Найди данные", lambda query, limit=10: {"ok": True, "results": []},
        lambda messages, **kwargs: seen.append(str(messages)) or next(replies),
    )
    assert result.steps == 1
    assert '"results": []' in seen[1]
    assert "Ничего не нашёл" in result.answer


def test_search_limit_argument_is_passed_through_the_react_schema():
    seen = []
    replies = iter([(_call("asyncio", 15), {}), ("Нашёл источники.", {})])
    result = _run(
        "Найди asyncio",
        lambda query, limit=10: seen.append(limit) or
        {"ok": True, "results": _items("asyncio", limit)},
        lambda *a, **k: next(replies),
    )
    assert seen == [15]
    assert result.steps == 1
