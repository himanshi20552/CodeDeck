from fastapi.testclient import TestClient
from types import SimpleNamespace
import sys

from app.main import app
from app.services import observability


def test_trace_redaction_hides_credentials():
    value = "api_key=abc password: secret Bearer eyJhbGciOiJabc"
    redacted = observability.redact_text(value)
    assert "abc" not in redacted
    assert "secret" not in redacted
    assert "[REDACTED]" in redacted


def test_llm_token_usage_is_forwarded_only_when_provider_reports_it():
    from app.services.repository_ai import _response_usage_details

    response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18))
    assert _response_usage_details(response) == {"input": 11, "output": 7, "total": 18}
    assert _response_usage_details(SimpleNamespace(usage=None)) is None


def test_optional_langfuse_is_disabled_without_credentials(monkeypatch):
    monkeypatch.setattr(observability.settings, "langfuse_public_key", "")
    monkeypatch.setattr(observability.settings, "langfuse_secret_key", "")
    assert observability.langfuse_status()["status"] == "disabled"
    with observability.trace_observation("test") as trace:
        assert trace is None


def test_langfuse_capture_is_content_safe_and_forwards_known_usage(monkeypatch):
    calls = {"kwargs": None, "updates": []}

    class FakeObservation:
        def update(self, **values):
            calls["updates"].append(values)

    class FakeContext:
        def __enter__(self):
            return FakeObservation()

        def __exit__(self, *_args):
            return False

    class FakeClient:
        def start_as_current_observation(self, **kwargs):
            calls["kwargs"] = kwargs
            return FakeContext()

    monkeypatch.setattr(observability.settings, "langfuse_public_key", "pk-test")
    monkeypatch.setattr(observability.settings, "langfuse_secret_key", "sk-test")
    monkeypatch.setattr(observability.settings, "langfuse_capture_content", False)
    monkeypatch.setitem(sys.modules, "langfuse", SimpleNamespace(get_client=lambda: FakeClient()))

    with observability.trace_observation("test-generation", as_type="generation", input_value="private question") as span:
        observability.update_observation(span, output="private answer", usage_details={"input": 10, "output": 4})

    assert "input" not in calls["kwargs"]
    assert calls["updates"] == [{"usage_details": {"input": 10, "output": 4}}]


def test_langfuse_export_error_does_not_break_the_request(monkeypatch):
    class BrokenClient:
        def start_as_current_observation(self, **_kwargs):
            raise OSError("private endpoint details")

    monkeypatch.setattr(observability.settings, "langfuse_public_key", "pk-test")
    monkeypatch.setattr(observability.settings, "langfuse_secret_key", "sk-test")
    monkeypatch.setitem(sys.modules, "langfuse", SimpleNamespace(get_client=lambda: BrokenClient()))
    with observability.trace_observation("test") as observation:
        assert observation is None


def test_metrics_labels_use_route_templates_and_bounded_methods():
    if not observability.PROMETHEUS_AVAILABLE:
        return
    from prometheus_client import generate_latest

    observability.record_http_request("GET", "/api/projects/{project_id}", 200, 0.01)
    output = generate_latest(observability.REGISTRY).decode("utf-8")
    assert 'route="/api/projects/{project_id}"' in output
    observability.record_http_request("CUSTOM-VERB", "unmatched", 200, 0.01)
    output = generate_latest(observability.REGISTRY).decode("utf-8")
    assert 'method="OTHER"' in output


def test_prometheus_endpoint_reports_real_request_metrics():
    if not observability.PROMETHEUS_AVAILABLE:
        return
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        first_scrape = client.get("/metrics")
        assert first_scrape.status_code == 200
        assert "codedeck_http_requests_total" in first_scrape.text
        assert 'route="/"' in first_scrape.text
        before = first_scrape.text.count('route="/metrics"')
        second_scrape = client.get("/metrics")
        assert second_scrape.status_code == 200
        assert second_scrape.text.count('route="/metrics"') == before
