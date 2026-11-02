import os
import subprocess

import pytest

from agent.github import GitWorkspaceError, clone_repository, commit, diff, publish


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def _bare(tmp_path):
    seed = tmp_path / "seed"
    bare = tmp_path / "origin.git"
    seed.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", "test@example.test")
    (seed / "README.md").write_text("base\n", encoding="utf-8")
    git(seed, "add", "README.md")
    git(seed, "commit", "-m", "initial")
    subprocess.run(["git", "clone", "--bare", str(seed), str(bare)], check=True, capture_output=True)
    return seed, bare


def _prepare(tmp_path, monkeypatch):
    seed, bare = _bare(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("AGENT_GITHUB_ALLOW_LOCAL", "1")
    clone_repository(str(bare), workspace, "run-42", allow_local=True)
    repo = workspace / "repository"
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    return workspace, repo, bare


def test_github_copy_branch_commit_and_diff_without_network(tmp_path, monkeypatch):
    workspace, repo, _bare_repo = _prepare(tmp_path, monkeypatch)
    assert git(repo, "branch", "--show-current") == "novamind/run-42"
    assert "README.md" in diff(workspace)["diff"]
    result = commit(workspace, "Update README")
    assert result["branch"] == "novamind/run-42"
    assert result["commit"]


def test_github_publish_requires_confirmation_and_never_pushes_main(tmp_path, monkeypatch):
    workspace, repo, bare = _prepare(tmp_path, monkeypatch)
    with pytest.raises(GitWorkspaceError, match="подтверждения"):
        publish(workspace, confirmed=False)
    git(repo, "checkout", "main")
    with pytest.raises(GitWorkspaceError, match="основной"):
        publish(workspace, confirmed=True)
    # No failed/unauthorized path changed the bare repository.
    assert git(bare, "rev-parse", "refs/heads/main") == git(repo, "rev-parse", "HEAD")


def test_github_push_targets_novamind_branch_only_after_confirmation(tmp_path, monkeypatch):
    workspace, repo, bare = _prepare(tmp_path, monkeypatch)
    commit(workspace, "Change in agent branch")
    published = publish(workspace, confirmed=True)
    assert published["pushed"] is True
    assert published["branch"] == "novamind/run-42"
    assert git(bare, "show-ref", "--verify", "refs/heads/novamind/run-42")
    assert git(bare, "rev-parse", "refs/heads/main") != git(repo, "rev-parse", "HEAD")
