"""Мультиагентные паттерны: конвейер, веер, критик, деградация маршрутов."""
import pytest

from agenticseek import orchestration


class FakeModel:
    """Скрипт ответов по роли: system-промпт определяет, кто отвечает."""

    def __init__(self, by_role, fail_roles=()):
        self.by_role = by_role
        self.fail_roles = set(fail_roles)
        self.calls = []

    def __call__(self, messages, system="", **kwargs):
        role = next((name for name, prompt in orchestration.ROLES.items()
                     if prompt == system), "unknown")
        self.calls.append({"role": role, "messages": messages, "system": system})
        if role in self.fail_roles:
            raise RuntimeError(f"{role} недоступен")
        value = self.by_role.get(role, f"ответ роли {role}")
        if callable(value):
            return value(messages), {}
        return value, {}


def test_sequential_passes_context_between_roles():
    model = FakeModel({"planner": "1. собрать\n2. написать",
                       "writer": "Итоговый ответ по плану"})
    result = orchestration.sequential(model, "сделай лендинг",
                                      roles=("planner", "writer"))
    assert result.ok
    assert result.answer == "Итоговый ответ по плану"
    assert result.pattern == "sequential"
    assert [step.role for step in result.steps] == ["planner", "writer"]
    # писатель получил план планировщика
    writer_call = next(call for call in model.calls if call["role"] == "writer")
    assert "1. собрать" in writer_call["messages"][-1]["content"]


def test_sequential_skips_failed_role():
    model = FakeModel({"writer": "Ответ без плана"}, fail_roles={"planner"})
    result = orchestration.sequential(model, "задача", roles=("planner", "writer"))
    assert result.answer == "Ответ без плана"
    assert result.steps[0].ok is False
    assert any("не дала текста" in note for note in result.notes)


def test_parallel_runs_roles_and_synthesises():
    model = FakeModel({"researcher": "факт 1\nфакт 2",
                       "analyst": "риск высокий",
                       "writer": "Сводка: факт 1, факт 2, риск высокий"})
    result = orchestration.parallel(model, "сравни варианты")
    assert result.pattern == "parallel"
    assert result.answer.startswith("Сводка")
    roles = [step.role for step in result.steps]
    assert roles[:2] == ["researcher", "analyst"]      # обе ветки выполнены
    assert roles[-1] == "writer"
    synthesis = next(call for call in model.calls if call["role"] == "writer")
    assert "факт 1" in synthesis["messages"][-1]["content"]
    assert "риск высокий" in synthesis["messages"][-1]["content"]


def test_parallel_returns_empty_when_all_branches_fail():
    model = FakeModel({}, fail_roles={"researcher", "analyst"})
    result = orchestration.parallel(model, "задача")
    assert result.ok is False
    assert "все ветки пусты" in " ".join(result.notes)


def test_critic_accepts_good_draft():
    model = FakeModel({"writer": "Готовый ответ", "critic": "ОК\nвсё по делу"})
    result = orchestration.critic(model, "напиши инструкцию")
    assert result.answer == "Готовый ответ"
    assert result.verdict.startswith("ОК")
    assert len(result.steps) == 2
    assert any("принято" in note for note in result.notes)


def test_critic_revises_after_findings():
    drafts = iter(["Черновик с ошибкой", "Исправленный ответ"])

    def writer(messages):
        return next(drafts)

    model = FakeModel({"writer": writer,
                       "critic": "1. исправь цифру 2. добавь источник"})
    result = orchestration.critic(model, "напиши отчёт", max_rounds=1)
    assert result.answer == "Исправленный ответ"
    assert any("есть замечания" in note for note in result.notes)


def test_critic_keeps_draft_when_revision_fails():
    model = FakeModel({"writer": "Черновик", "critic": "1. переделай"})
    result = orchestration.critic(model, "задача", max_rounds=1)
    assert result.answer == "Черновик"


def test_critic_without_draft_is_empty():
    model = FakeModel({"writer": ""})
    result = orchestration.critic(model, "задача")
    assert result.ok is False


def test_router_falls_back_to_next_chain():
    model = FakeModel({"researcher": "", "writer": "Ответ второго маршрута"})
    result = orchestration.router(model, "задача",
                                  chains=[("researcher",), ("writer",)])
    assert result.answer == "Ответ второго маршрута"
    assert any("использован маршрут 2" in note for note in result.notes)


def test_router_reports_empty_when_all_fail():
    model = FakeModel({}, fail_roles=set(orchestration.ROLES))
    result = orchestration.router(model, "задача", chains=[("writer",), ("critic",)])
    assert result.ok is False
    assert "все маршруты пусты" in " ".join(result.notes)


@pytest.mark.parametrize("goal,expected", [
    ("Сравни PostgreSQL и SQLite: плюсы и минусы", "parallel"),
    ("Проверь мой план и скажи, где он сломается", "critic"),
    ("Исследуй рынок и собери факты", "sequential"),
])
def test_choose_pattern(goal, expected):
    assert orchestration.choose_pattern(goal) == expected


def test_run_off_returns_empty():
    result = orchestration.run(lambda *a, **k: ("x", {}), "задача", pattern="off")
    assert result.ok is False
    assert result.pattern == "off"


def test_run_auto_picks_pattern_and_emits_events():
    events = []
    model = FakeModel({"researcher": "факт", "analyst": "мнение", "writer": "итог"})
    result = orchestration.run(model, "Сравни два варианта и напиши вывод",
                               pattern="auto", emit=events.append)
    assert result.pattern == "parallel"
    assert result.answer == "итог"
    assert any(event.get("type") == "stage" for event in events)
    assert any(event.get("type") == "step" for event in events)


def test_run_unknown_pattern_falls_back_to_auto():
    model = FakeModel({"writer": "ответ"})
    result = orchestration.run(model, "короткий вопрос", pattern="нечто")
    assert result.pattern in ("auto", "sequential", "parallel", "critic")


def test_result_as_dict_is_serialisable():
    model = FakeModel({"writer": "ответ"})
    result = orchestration.sequential(model, "задача", roles=("writer",))
    data = result.as_dict()
    assert data["pattern"] == "sequential"
    assert data["steps"][0]["role"] == "writer"
    assert data["chars"] == len("ответ")


def test_summary_lists_roles(monkeypatch):
    monkeypatch.setenv("AGENT_PATTERN", "auto")
    data = orchestration.summary()
    assert "critic" in data["roles"]
    assert "parallel" in data["patterns"]
