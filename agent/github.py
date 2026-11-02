"""Constrained Git operations for repository copies inside an agent workspace."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


class GitWorkspaceError(RuntimeError):
    pass


def _env():
    # Git child processes never inherit provider/MCP/API credentials.
    keep = {"PATH", "HOME", "USER", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}
    env = {key: value for key, value in os.environ.items() if key in keep}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null"})
    return env


def _git(cwd: Path, *args: str, timeout: int = 30) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=str(cwd), env=_env(),
                                capture_output=True, text=True, timeout=timeout,
                                check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitWorkspaceError(f"Git command failed: {type(exc).__name__}") from exc
    if result.returncode:
        raise GitWorkspaceError((result.stderr or result.stdout or "git error")[-1200:])
    return (result.stdout or "").strip()


def validate_source(source: str, *, allow_local: bool = False) -> str:
    value = str(source or "").strip()
    if re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?", value):
        return value
    if allow_local and (Path(value).exists() or value.startswith("file://")):
        return value
    raise GitWorkspaceError("Укажите HTTPS URL публичного GitHub-репозитория")


def clone_repository(source: str, workspace: Path, run_id: str, *, allow_local=False) -> dict:
    source = validate_source(source, allow_local=allow_local)
    run_key = re.sub(r"[^A-Za-z0-9_-]", "", str(run_id or ""))[:48]
    if not run_key:
        raise GitWorkspaceError("Не задан идентификатор запуска")
    dest = (workspace / "repository").resolve()
    if not dest.is_relative_to(workspace.resolve()):
        raise GitWorkspaceError("Каталог репозитория вне workspace")
    if dest.exists():
        raise GitWorkspaceError("Копия репозитория уже существует в этой сессии")
    result = subprocess.run(["git", "clone", "--", source, str(dest)], cwd=str(workspace),
                            env=_env(), capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise GitWorkspaceError((result.stderr or result.stdout or "clone failed")[-1200:])
    branch = f"novamind/{run_key}"
    _git(dest, "checkout", "-b", branch)
    _git(dest, "config", "user.name", "NovaMind Agent")
    _git(dest, "config", "user.email", "novamind-agent@localhost")
    return {"path": "repository", "branch": branch,
            "source": source, "head": _git(dest, "rev-parse", "HEAD")}


def repository_path(workspace: Path) -> Path:
    dest = (workspace / "repository").resolve()
    if not dest.is_relative_to(workspace.resolve()) or not (dest / ".git").exists():
        raise GitWorkspaceError("Скопированный репозиторий не найден")
    return dest


def diff(workspace: Path) -> dict:
    repo = repository_path(workspace)
    return {"branch": _git(repo, "branch", "--show-current"),
            "diff": _git(repo, "diff", "--no-ext-diff", "--", "."),
            "status": _git(repo, "status", "--short")}


def commit(workspace: Path, message: str) -> dict:
    repo = repository_path(workspace)
    branch = _git(repo, "branch", "--show-current")
    if not branch.startswith("novamind/") or branch in {"main", "master"}:
        raise GitWorkspaceError("Коммит разрешён только в ветке novamind/<run-id>")
    _git(repo, "add", "-A")
    status = _git(repo, "status", "--short")
    if not status:
        raise GitWorkspaceError("Нет изменений для коммита")
    _git(repo, "commit", "-m", str(message or "Изменения NovaMind")[:160])
    return {"branch": branch, "commit": _git(repo, "rev-parse", "HEAD"),
            "summary": _git(repo, "show", "--stat", "--oneline", "HEAD")}


def publish(workspace: Path, *, confirmed: bool, base: str = "main") -> dict:
    repo = repository_path(workspace)
    branch = _git(repo, "branch", "--show-current")
    if not confirmed:
        raise GitWorkspaceError("Публикация требует явного подтверждения пользователя")
    if base.lower() in {"main", "master"} and branch.lower() in {"main", "master"}:
        raise GitWorkspaceError("Публикация из основной ветки запрещена")
    if not branch.startswith("novamind/"):
        raise GitWorkspaceError("Пуш разрешён только из ветки novamind/<run-id>")
    remotes = _git(repo, "remote")
    if not remotes:
        raise GitWorkspaceError("У репозитория нет remote")
    _git(repo, "push", "origin", f"HEAD:refs/heads/{branch}", timeout=120)
    return {"branch": branch, "base": base, "pushed": True}
