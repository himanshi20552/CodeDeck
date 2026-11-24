from app.services.repository_env import repository_child_environment
from app.agents.testing_agent import _child_env as get_testing_child_environment
from app.services.repository_ai import _child_env as repository_ai_child_env


def test_repository_environment_keeps_runtime_but_strips_credentials():
    source = {
        "PATH": "tools",
        "LANG": "en_US.UTF-8",
        "OPENAI_API_KEY": "secret",
        "RELEASE_APPROVAL_TOKEN": "secret",
        "REMOTE_WORKER_TOKEN": "secret",
        "GROQ_API_KEY": "secret",
        "DATABASE_PASSWORD": "secret",
        "DATABASE_URL": "postgres://user:secret@host/db",
        "KUBECONFIG": "private-config",
        "AWS_ACCESS_KEY_ID": "secret",
        "APP_MODE": "test",
    }
    assert repository_child_environment(source) == {"PATH": "tools", "LANG": "en_US.UTF-8", "APP_MODE": "test"}


def test_test_runners_use_filtered_environment(monkeypatch):
    monkeypatch.setenv("RELEASE_APPROVAL_TOKEN", "secret")
    monkeypatch.setenv("KUBECONFIG", "private-config")
    monkeypatch.setenv("CI", "true")
    for env in (get_testing_child_environment(), repository_ai_child_env()):
        assert "RELEASE_APPROVAL_TOKEN" not in env
        assert "KUBECONFIG" not in env
        assert env.get("CI") == "true"
