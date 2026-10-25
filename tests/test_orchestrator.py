from agenticseek.engine import AgentEngine, EngineContext
from agenticseek.core.orchestrator_agent import OrchestratorAgent


def build(monkeypatch, **kwargs):
    monkeypatch.setenv('AGENTICSEEK_EXECUTION_MODE', 'unified')
    ctx = EngineContext(goal='задача', complete=lambda *a, **k: ('Готово', {}),
                        emit=lambda e: None, **kwargs)
    engine = AgentEngine(ctx)
    engine._build_agents()
    return engine


def test_single_model_gets_only_available_tools(monkeypatch):
    engine = build(monkeypatch, search=lambda *a, **k: ([], {}))
    agent = engine.router.select_agent('найди, прочитай и исправь')
    assert isinstance(agent, OrchestratorAgent)
    assert len(engine.agents) == 1
    assert 'web_search' in agent.all_tools()
    assert 'terminal' not in agent.all_tools()
    assert 'mcp_call' not in agent.all_tools()


def test_workspace_has_files_python_and_terminal(monkeypatch):
    engine = build(monkeypatch, workspace=True, max_steps=27)
    agent = engine.router.select_agent('исправь код')
    assert agent.max_turns == 27
    assert {'terminal', 'python', 'file_read', 'file_write'} <= set(agent.all_tools())
    assert 'nova tools' in agent.system_prompt()


def test_explicit_web_role_keeps_narrow_boundary(monkeypatch):
    engine = build(monkeypatch, workspace=True, force_role='web')
    agent = engine.router.by_role('web')
    assert not isinstance(agent, OrchestratorAgent)
    assert 'terminal' not in agent.all_tools()


def test_legacy_mode_available(monkeypatch):
    monkeypatch.setenv('AGENTICSEEK_EXECUTION_MODE', 'specialists')
    ctx = EngineContext(goal='test', complete=lambda *a, **k: ('ok', {}), emit=lambda e: None)
    engine = AgentEngine(ctx)
    engine._build_agents()
    assert len(engine.agents) > 1


def test_observation_fed_back_before_next_action(monkeypatch):
    replies = iter(['```web_search\nexample\n```', 'Найдено: результат проверки.'])
    seen = []
    def complete(messages, **kwargs):
        seen.append(str(messages))
        return next(replies), {}
    monkeypatch.setenv('AGENTICSEEK_EXECUTION_MODE', 'unified')
    events = []
    ctx = EngineContext(goal='найди example', complete=complete, emit=events.append,
                        search=lambda *a, **k: ([{'title': 'результат проверки',
                                                 'url': 'https://example.com',
                                                 'snippet': 'данные'}], {}))
    engine = AgentEngine(ctx)
    engine._build_agents()
    agent = engine.router.select_agent(ctx.goal)
    answer, _ = agent.process(ctx.goal)
    assert 'Найдено' in answer
    assert len(seen) == 2
    assert 'результат проверки' in seen[1]
    assert any(e.get('type') == 'tool' for e in events)
