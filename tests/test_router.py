"""Маршрутизация: кто именно выполняет запрос.

Пользовательский сценарий: «создай интернет-магазин на Flask» уходил
веб-агенту (слова «сайт» и «интернет» — маркеры поиска), а тот умеет только
искать страницы. Итог — «Браузер недоступен» / просьба разрешения и никакой
работы. Эти тесты фиксируют две вещи: создание кода идёт программисту, и если
модель всё же вызвала инструмент не у того исполнителя, ход передаётся тому,
у кого инструмент есть.
"""
import pytest

from agenticseek.router import AgentRouter, is_build_request, is_mcp_search_request
from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


@pytest.fixture(autouse=True)
def specialists_mode(monkeypatch):
    """Тесты планировщика и передачи хода написаны под архитектуру специалистов.

    В проде по умолчанию ``unified`` (один оркестратор), его проверяет
    ``test_orchestrator.py``. Здесь нужны planner и роли web/code/files/talk.
    """
    monkeypatch.setenv("AGENTICSEEK_EXECUTION_MODE", "specialists")


class _Agent:
    def __init__(self, role, tools, label=None):
        self.role = role
        self.type = f"{role}_agent"
        self.tools = dict.fromkeys(tools, object())
        self.label = label or role

    def available_tools(self):
        return list(self.tools)


def _router():
    return AgentRouter([_Agent("talk", []), _Agent("code", ["bash", "python"]),
                        _Agent("files", ["file_edit"]), _Agent("web", ["web_search"])],
                       mode="heuristic")


@pytest.mark.parametrize("text", [
    "Создай интернет-магазин на Flask",
    "создай сайт",
    "напиши сайт на flask с корзиной",
    "сделай бота для телеграма",
    "напиши парсер цен",
])
def test_build_requests_go_to_coder(text):
    assert is_build_request(text) is True
    assert _router().select_agent(text).role == "code"


@pytest.mark.parametrize("text", [
    "найди сайт с рецептами",
    "что пишут на сайте новостей",
    "найди в интернете погоду в Худжанде",
    "сколько стоит билет",
    "найди, как создать сайт на flask",
    "открой сайт example.com",
])
def test_search_requests_still_go_to_web(text):
    assert is_build_request(text) is False
    assert _router().select_agent(text).role == "web"


def test_short_message_goes_to_talk():
    assert _router().select_agent("привет").role == "talk"


ROUTER_10_CASES = [
    # 1. Границы слов (латиница): «digital» не должен давать балл «git».
    ("explain digital marketing strategy", "talk"),
    # 2. Границы слов + суффикс: «курсор» не должен срабатывать как «курс».
    ("мигает курсор на экране телефона", "talk"),
    # 3. Русские окончания: «курсы» совпадает с «курс».
    ("покажи свежие курсы валют в банке", "web"),
    # 4. Русские окончания во множественном числе и падежах для files.
    ("прочитайте файлы в рабочих папках", "files"),
    # 5. Русские окончания для code («исправьте», «ошибки», «функциях»).
    ("исправьте ошибки в функциях", "code"),
    # 6. Равенство баллов (1 у code «баг», 1 у web «найди») → приоритет code.
    ("найди баг в модуле", "code"),
    # 7. Равенство баллов (1 у files «файл», 1 у web «найди») → приоритет files.
    ("найди файл конфигурации", "files"),
    # 8. Запрос с «mcp» и глаголом поиска → агент с MCP-инструментами (files).
    ("найди через mcp подключённые инструменты", "files"),
    # 9. Запрос с «github» и глаголом поиска → агент с MCP-инструментами (files).
    ("поищи на github программы для анализа логов", "files"),
    # 10. Настоящее слово «git» по границам слова → code.
    ("проверь ветку через git в терминале", "code"),
]


@pytest.mark.parametrize(("phrase", "expected_role"), ROUTER_10_CASES)
def test_router_10_phrases_expected_roles(phrase, expected_role):
    router = AgentRouter([
        _Agent("talk", []),
        _Agent("code", ["bash", "python", "mcp_call"]),
        _Agent("files", ["file_edit", "mcp_call"]),
        _Agent("web", ["web_search", "mcp_call"]),
    ])
    assert router.mode == "heuristic"
    assert router.select_agent(phrase).role == expected_role


def test_mcp_search_picks_whichever_agent_actually_has_mcp_call():
    """Если mcp_call зарегистрирован только у одного агента, роутер выбирает именно его."""
    router = AgentRouter([
        _Agent("talk", []),
        _Agent("code", ["bash", "mcp_call"]),
        _Agent("files", ["file_edit"]),
        _Agent("web", ["web_search"]),
    ])
    assert is_mcp_search_request("найди репозитории на github") is True
    assert router.select_agent("найди репозитории на github").role == "code"


# ══════════════ передача хода тому, у кого есть инструмент ══════════════

def test_web_agent_cannot_run_bash_block(admin_client, sandbox, monkeypatch):
    """Веб-агент не получает bash — команду выполняет программист."""
    seen = []
    script(monkeypatch, [
        "```bash\nprintf 'made by coder' > report.txt\n```",
        "Готово: файл создан.",
    ], seen)
    events = ndjson(admin_client.post(
        "/api/agent/stream", json={"message": "найди в интернете погоду в Худжанде"}))
    stages = [event.get("title", "") + event.get("text", "") for event in events_of(events, "stage")]
    assert any("Веб-агент" in stage for stage in stages)
    steps = [event.get("text", "") for event in events_of(events, "step")]
    assert "Передаю задачу исполнителю: Программист" in steps
    tools = events_of(events, "tool")
    assert tools and tools[0]["name"] == "terminal"
    assert (sandbox / "report.txt").read_text(encoding="utf-8").strip() == "made by coder"
    assert answer_of(events) == "Готово: файл создан."


def test_talk_agent_does_not_execute_shell_example(admin_client, sandbox, monkeypatch):
    """В разговоре блок ```bash — пример, а не действие: ничего не запускаем."""
    seen = []
    script(monkeypatch, [
        "```bash\nrm -rf .\n```",
        "Это пример команды, я её не запускал.",
    ], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    assert not events_of(events, "tool")
    assert (sandbox / "app.py").parent.exists()  # песочница цела
    steps = [event.get("text", "") for event in events_of(events, "step")]
    assert any("не выполнялись" in step for step in steps)
