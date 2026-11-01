from pathlib import Path
import base64
from subprocess import CompletedProcess, TimeoutExpired

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


def test_private_clone_keeps_token_out_of_git_remote_url(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    destination = workspace / "repo"
    monkeypatch.setattr(github.settings, "workspace_dir", str(workspace))
    monkeypatch.setattr(github.settings, "github_token", "ghp_example_secret_value_1234567890")
    captured = {}

    def clone(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(github.subprocess, "run", clone)
    github.clone_repository("http://github.com/acme/repo", destination)

    assert captured["args"][5] == "https://github.com/acme/repo.git"
    assert all("ghp_example_secret_value" not in arg for arg in captured["args"])
    assert captured["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "ghp_example_secret_value" not in captured["env"]["GIT_CONFIG_VALUE_0"]
    assert base64.b64encode(b"x-access-token:ghp_example_secret_value_1234567890").decode() in captured["env"]["GIT_CONFIG_VALUE_0"]
