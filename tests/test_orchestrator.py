"""ReAct tool visibility is computed per run from one authoritative registry."""
from agent.loop import AgentContext, _specs
from tool_registry import ToolSpec


def _spec(name, permissions=()):
    return ToolSpec(name, name, {"type": "object", "properties": {}},
                    lambda **kwargs: {"ok": True}, permissions=permissions)


def test_non_workspace_run_hides_file_and_shell_tools():
    specs = [_spec("web_search", ("network",)), _spec("file_read", ("workspace",)),
             _spec("shell", ("workspace",)), _spec("ask_user")]
    available = _specs(AgentContext("поиск", tools=specs, workspace=False))
    assert {spec.name for spec in available} == {"web_search", "ask_user"}


def test_workspace_run_exposes_only_tools_granted_to_that_run():
    specs = [_spec("web_search", ("network",)), _spec("file_read", ("workspace",)),
             _spec("shell", ("workspace",)), _spec("mcp_call", ("mcp",))]
    context = AgentContext("исправь файл", tools=specs, workspace=True)
    available = _specs(context)
    assert {spec.name for spec in available} == {"web_search", "file_read", "shell", "mcp_call"}


def test_explicit_tool_subset_is_not_expanded_by_workspace_access():
    specs = [_spec("web_search"), _spec("file_read", ("workspace",)),
             _spec("file_write", ("workspace",))]
    available = _specs(AgentContext("читай", tools=[specs[1]], workspace=True))
    assert [spec.name for spec in available] == ["file_read"]


def test_empty_toolset_remains_empty():
    assert _specs(AgentContext("текстовый вопрос", tools=[], workspace=True)) == []
