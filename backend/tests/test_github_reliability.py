from pathlib import Path
from subprocess import TimeoutExpired

import pytest

from app.services import github


def test_clone_timeout_cleans_partial_checkout(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    destination = workspace / "project_1"
    destination.mkdir(parents=True)
    (destination / "partial.txt").write_text("partial", encoding="utf-8")
    monkeypatch.setattr(github.settings, "workspace_dir", str(workspace))
    monkeypatch.setattr(github.settings, "repository_clone_timeout", 10)

    def timeout(*_args, **_kwargs):
        raise TimeoutExpired("git clone", 10)

    monkeypatch.setattr(github.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="exceeded the 10s timeout"):
        github.clone_repository("https://github.com/acme/repo", destination)
    assert not destination.exists()


def test_clone_rejects_workspace_prefix_traversal(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "work"
    sibling = tmp_path / "workspace-outside"
    monkeypatch.setattr(github.settings, "workspace_dir", str(workspace))
    with pytest.raises(ValueError, match="outside workspace"):
        github.clone_repository("https://github.com/acme/repo", sibling / "repo")
