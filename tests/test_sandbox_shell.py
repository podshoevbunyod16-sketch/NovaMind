"""Терминал песочницы: конвейеры, цепочки, перенаправления — и границы."""
import os
import shutil

import pytest

import routes.terminal as term_routes
from sandbox_shell import Shell, ShellError, parse


def _run(client, command):
    return client.post("/api/terminal/run", json={"command": command})


def _ok(client, command):
    response = _run(client, command)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


# ══════════════ через API (как это делает ИИ) ══════════════

def test_pipe_and_chain(admin_client):
    data = _ok(admin_client, "echo привет мир | tr ' ' '\\n' | wc -l && echo готово")
    assert data["code"] == 0
    assert data["stdout"].split() == ["2", "готово"]


def test_or_chain_runs_fallback(admin_client):
    data = _ok(admin_client, "ls нет-такого 2>/dev/null || echo запасной")
    assert data["stdout"].strip() == "запасной"


def test_redirect_into_workspace_file(admin_client, sandbox):
    _ok(admin_client, "python3 -c \"print(2**10)\" > notes/pow.txt 2>&1")
    assert (sandbox / "notes" / "pow.txt").read_text().strip() == "1024"
    data = _ok(admin_client, "echo ещё >> notes/pow.txt ; cat < notes/pow.txt")
    assert data["stdout"].split() == ["1024", "ещё"]


def test_query_string_with_ampersand_is_not_a_shell_operator(admin_client):
    data = _ok(admin_client, 'echo "https://api.example.com/x?a=1&b=2|3"')
    assert data["stdout"].strip() == "https://api.example.com/x?a=1&b=2|3"


def test_cd_and_globs(admin_client):
    data = _ok(admin_client, "mkdir -p src && touch src/a.py src/b.py src/c.txt && cd src && ls *.py")
    assert data["stdout"].split() == ["a.py", "b.py"]


def test_multiline_script_and_comments(admin_client):
    data = _ok(admin_client, "echo один  # комментарий\necho два")
    assert data["stdout"].split() == ["один", "два"]


@pytest.mark.skipif(not shutil.which("git"), reason="git не установлен")
def test_git_workflow_in_one_line(admin_client):
    data = _ok(admin_client, "git init -q && git add . && git commit -q -m старт && git log --oneline | wc -l")
    assert data["code"] == 0, data["stderr"]
    assert data["stdout"].strip() == "1"


@pytest.mark.skipif(not shutil.which("curl"), reason="curl не установлен")
def test_curl_is_available(admin_client):
    data = _ok(admin_client, "curl --version | head -1")
    assert data["stdout"].startswith("curl ")


def test_nova_search_can_be_piped(admin_client, monkeypatch):
    results = [{"title": "Python", "url": "https://python.org", "snippet": ""},
               {"title": "PyPI", "url": "https://pypi.org", "snippet": ""}]
    monkeypatch.setattr(term_routes, "nova_search", lambda query, limit=8: (
        results, {"backend": "test", "elapsed_ms": 1, "query": query, "results": results,
                  "count": 2, "trace": []}))
    data = _ok(admin_client, "nova search python | grep -c https")
    assert data["stdout"].strip() == "2"


def test_env_of_commands_has_no_server_secrets(admin_client, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "секрет")
    data = _ok(admin_client, "env")
    assert "секрет" not in data["stdout"] and "HOME=" in data["stdout"]


@pytest.mark.parametrize("command", [
    "cat README.md | bash",                 # программа вне списка в конвейере
    "env sh -c id",                         # спрятана за env
    "find . -exec sh {} ;",                 # спрятана за find -exec
    "echo hi > /etc/hosts",                 # запись вне папки
    "rm -rf /",                             # удаление вне папки
    "curl -o /tmp/x https://example.com",   # скачивание вне папки
    "git -C / status",                      # git вне папки
    "cat notes.md | xargs rm",              # пути из stdin — не проверить
    "echo $(whoami)",                       # подстановка
    "sleep 10 &",                           # фон
    "cat <<EOF",                            # here-doc
    "ls |",                                 # обрыв конвейера
])
def test_unsafe_lines_are_rejected_before_running(admin_client, command):
    response = _run(admin_client, command)
    assert response.status_code == 400, response.get_json()


def test_relative_escape_is_stopped_at_runtime(admin_client, sandbox):
    data = _ok(admin_client, "echo x > ../escape.txt")
    assert data["code"] != 0 and "пределы" in data["stderr"]
    assert not (sandbox.parent / "escape.txt").exists()
    data = _ok(admin_client, "cd ..")
    assert data["code"] != 0


def test_failed_chain_stops_on_and(admin_client, sandbox):
    data = _ok(admin_client, "false && touch should-not-exist")
    assert data["code"] == 1
    assert not (sandbox / "should-not-exist").exists()


def test_runaway_pipeline_is_killed(admin_client, monkeypatch):
    monkeypatch.setattr(term_routes, "COMMAND_TIMEOUT", "1")
    data = _ok(admin_client, "yes | grep -c нет")          # молчит и никогда не кончится
    assert data["timed_out"] is True and data["code"] == 124


def test_flood_of_output_is_stopped(admin_client):
    data = _ok(admin_client, "yes")
    assert "слишком большой" in data["stderr"]
    assert len(data["stdout"]) < 20000 and "обрезан" in data["stdout"]


# ══════════════ сам разборщик ══════════════

def test_parse_structure():
    chains = parse('A=1 python3 x.py "a b" > out.txt 2>&1 | wc -l && echo ok', {})
    assert [joiner for joiner, _ in chains] == [None, "&&"]
    first = chains[0][1]
    assert first[0]["assign"] == ["A=1"]
    assert first[0]["argv"] == ["python3", "x.py", "a b"]
    assert first[0]["redirects"] == [(">", "out.txt"), ("2>&1", None)]
    assert first[1]["argv"] == ["wc", "-l"]


def test_quotes_and_variables():
    env = {"HOME": "/w", "NAME": "мир"}
    chains = parse("echo '$HOME' \"$NAME!\" ${HOME}/x ~ a\\ b", env)
    assert chains[0][1][0]["argv"] == ["echo", "$HOME", "мир!", "/w/x", "/w", "a b"]


def test_digit_glued_to_word_is_not_a_redirect():
    chains = parse("echo file2>out", {})
    assert chains[0][1][0]["argv"] == ["echo", "file2"]
    assert chains[0][1][0]["redirects"] == [(">", "out")]


def test_shell_validate_without_flask(tmp_path):
    shell = Shell(str(tmp_path), {"echo"}, {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)})
    with pytest.raises(ShellError):
        shell.validate("echo hi | cat")
    result = shell.run("echo hi")
    assert result["stdout"] == "hi\n" and result["code"] == 0
