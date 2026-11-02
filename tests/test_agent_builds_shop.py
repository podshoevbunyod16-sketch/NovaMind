"""End-to-end project creation through first-party workspace tools and ReAct."""
from __future__ import annotations

from pathlib import Path

import pytest

SHOP_DIR = Path(__file__).resolve().parent.parent / "examples" / "flask_shop"
SMOKE = '''"""Проверка магазина без запуска сервера."""
import sys
sys.path.insert(0, "shop")
from app import create_app
client = create_app("shop/data/smoke.db").test_client()
assert client.get("/").status_code == 200, "каталог"
assert client.get("/healthz").get_json()["products"] >= 3, "база"
client.post("/cart/add/1", data={"quantity": 2})
assert b"1 580" in client.get("/cart").data, "корзина и сумма"
order = client.post("/checkout", data={"customer": "Тест", "phone": "1", "address": "2"})
assert order.status_code == 302 and "/order/" in order.headers["Location"], "заказ"
assert client.get("/admin").status_code == 403, "админка закрыта"
print("SMOKE OK")
'''


@pytest.fixture()
def pipeline(tmp_path, monkeypatch):
    work = tmp_path / "ws"
    work.mkdir()
    monkeypatch.setenv("TERMINAL_DIR", str(work))
    monkeypatch.setenv("TERMINAL_ENABLED", "1")
    from tool_registry import bootstrap_default_tools
    bootstrap_default_tools()
    return work


def _call_as_owner(work, name, **arguments):
    """Call a registry tool with request identity, capability and workspace context."""
    from flask import session
    from app import app
    import capabilities
    import identity
    import tool_registry
    from agent.workspace import bind_workspace, reset_workspace

    with app.test_request_context("/"):
        session["admin_logged_in"] = True
        owner = identity.chat_owner()
        _token, capability = capabilities.mint(owner=owner, run_id="test-workspace",
                                                role="admin", tools=[s.name for s in tool_registry.list_tools()])
        cap_context = capabilities.activate(capability)
        workspace_context = bind_workspace(work)
        try:
            return tool_registry.call_tool(name, **arguments)
        finally:
            reset_workspace(workspace_context)
            capabilities.deactivate(cap_context)


def _write(work, relative, text):
    return _call_as_owner(work, "file_write", path=relative, content=text)


def test_agent_writes_project_and_smoke_test_passes(pipeline):
    work = pipeline
    assert _write(work, "shop/app.py", (SHOP_DIR / "app.py").read_text(encoding="utf-8"))["ok"]
    for template in sorted((SHOP_DIR / "templates").glob("*.html")):
        assert _write(work, f"shop/templates/{template.name}",
                      template.read_text(encoding="utf-8"))["ok"]
    assert _write(work, "shop/smoke.py", SMOKE)["ok"]
    assert (work / "shop" / "app.py").is_file()
    assert len(list((work / "shop" / "templates").glob("*.html"))) >= 6
    smoke = _call_as_owner(work, "shell", command="python3 shop/smoke.py")
    assert smoke["ok"] and "SMOKE OK" in smoke.get("stdout", "")


def test_agent_cannot_silently_overwrite_a_file(pipeline):
    work = pipeline
    assert _write(work, "shop/notes.txt", "первая версия")["ok"]
    result = _write(work, "shop/notes.txt", "вторая версия")
    assert result["ok"] is False
    assert "file_edit" in result["error"]["message"]
    assert (work / "shop" / "notes.txt").read_text(encoding="utf-8") == "первая версия"


def test_agent_shell_sees_written_files(pipeline):
    work = pipeline
    assert _write(work, "app.py", "print('shop')\n")["ok"]
    result = _call_as_owner(work, "shell", command="ls -1")
    assert result["ok"] and "app.py" in result["stdout"]


def test_agent_rejects_absolute_paths_in_commands(pipeline):
    result = _call_as_owner(pipeline, "shell", command="ls /etc")
    assert result["ok"] is False
    assert "относительн" in result["error"]["message"]
    assert "Path traversal" not in result["error"]["message"]


def _read_events(response):
    import json
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def test_full_turn_builds_the_shop_in_one_goal(admin_client, sandbox, monkeypatch):
    """A single goal creates all project files, then separately runs a real smoke test."""
    from tests.agent_helpers import answer_of, events_of, script
    blocks = [f"```python:shop/app.py\n{(SHOP_DIR / 'app.py').read_text(encoding='utf-8')}\n```"]
    for template in sorted((SHOP_DIR / "templates").glob("*.html")):
        blocks.append(f"```python:shop/templates/{template.name}\n"
                      f"{template.read_text(encoding='utf-8')}\n```")
    blocks.append(f"```python:shop/smoke.py\n{SMOKE}\n```")
    script(monkeypatch, ["\n".join(blocks), "```bash\npython3 shop/smoke.py\n```",
                         "Готово: магазин собран, проверка прошла."])
    events = _read_events(admin_client.post("/api/agent/react", json={
        "message": "Создай веб сайт интернет магазина с помощью питона и фласк",
        "workspace": True,
    }))
    assert (sandbox / "shop" / "app.py").is_file()
    assert len(list((sandbox / "shop" / "templates").glob("*.html"))) >= 6
    assert (sandbox / "shop" / "smoke.py").is_file()
    tools = events_of(events, "tool")
    assert all(tool["result"].get("ok") for tool in tools), tools
    assert tools[-1]["name"] == "shell" and "SMOKE OK" in tools[-1]["result"]["stdout"]
    assert "магазин собран" in answer_of(events)


def test_model_sees_failure_edits_project_and_verifies_again(admin_client, sandbox, monkeypatch):
    """A tool failure is returned as an observation and a later verification may retry."""
    import json
    from tests.agent_helpers import answer_of, events_of, script

    source = (SHOP_DIR / "app.py").read_text(encoding="utf-8")
    broken = source.replace(
        "from flask import (Flask, abort, flash, g, redirect, render_template, request,\n                   session, url_for)",
        "from flask import (Flask, abort, flash, g, redirect, render_template, request,")
    assert broken != source
    (sandbox / "shop").mkdir()
    (sandbox / "shop" / "app.py").write_text(broken, encoding="utf-8")
    for template in sorted((SHOP_DIR / "templates").glob("*.html")):
        target = sandbox / "shop" / "templates" / template.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    (sandbox / "shop" / "smoke.py").write_text(SMOKE, encoding="utf-8")
    fix = ('```file_edit\n' + json.dumps({
        "path": "shop/app.py",
        "old": "from flask import (Flask, abort, flash, g, redirect, render_template, request,",
        "new": "from flask import (Flask, abort, flash, g, redirect, render_template, request,\n                   session, url_for)",
    }, ensure_ascii=False) + '\n```')
    seen = []
    script(monkeypatch, ["```bash\npython3 shop/smoke.py\n```", fix,
                         "```bash\npython3 shop/smoke.py\n```",
                         "Исправил импорт, повторная проверка проходит."], seen)
    events = _read_events(admin_client.post("/api/agent/react", json={
        "message": "Проверь магазин, исправь обнаруженную ошибку и перепроверь",
        "workspace": True,
    }))
    tools = events_of(events, "tool")
    smoke_runs = [tool for tool in tools if tool["name"] == "shell"]
    assert len(smoke_runs) == 2, tools
    assert smoke_runs[0]["result"]["ok"] is False
    assert "SyntaxError" in str(smoke_runs[0]["result"])
    assert any("SyntaxError" in str(message.get("content"))
               for call in seen for message in call["messages"])
    edits = [tool for tool in tools if tool["name"] == "file_edit"]
    assert edits and edits[0]["result"]["ok"] is True
    assert smoke_runs[1]["result"]["ok"] is True
    assert "повторная проверка проходит" in answer_of(events)
