"""Optional Langfuse tracing and low-cardinality Prometheus metrics."""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from functools import wraps
from typing import Any, Iterator

from app.core.config import settings

logger = logging.getLogger("codedeck.observability")

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        REGISTRY,
        Counter,
        Gauge,
        Histogram,
        make_asgi_app,
    )

    PROMETHEUS_AVAILABLE = True
except ImportError:  # Optional diagnostics must not prevent the API starting.
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    REGISTRY = None
    Counter = Gauge = Histogram = None  # type: ignore[assignment,misc]
    make_asgi_app = None  # type: ignore[assignment]
    PROMETHEUS_AVAILABLE = False


if PROMETHEUS_AVAILABLE:
    HTTP_REQUESTS = Counter(
        "codedeck_http_requests_total",
        "Completed HTTP requests, excluding metrics scrapes.",
        ("method", "route", "status"),
    )
    HTTP_DURATION = Histogram(
        "codedeck_http_request_duration_seconds",
        "HTTP request duration in seconds, excluding metrics scrapes.",
        ("method", "route"),
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    )
    OPERATIONS = Counter(
        "codedeck_operations_total",
        "Completed CodeDeck operations by bounded operation and outcome.",
        ("operation", "outcome"),
    )
    OPERATION_DURATION = Histogram(
        "codedeck_operation_duration_seconds",
        "Duration of CodeDeck operations in seconds.",
        ("operation", "outcome"),
        buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 180, 600, 1800),
    )
    ACTIVE_OPERATIONS = Gauge(
        "codedeck_active_operations",
        "Number of currently running long operations.",
        ("operation",),
    )
    LLM_REQUESTS = Counter(
        "codedeck_llm_requests_total",
        "LLM requests by configured provider family and outcome.",
        ("provider", "outcome"),
    )
    LLM_DURATION = Histogram(
        "codedeck_llm_request_duration_seconds",
        "LLM request duration in seconds.",
        ("provider",),
        buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60),
    )


_SAFE_OPERATIONS = {
    "repository_index",
    "repository_retrieval",
    "llm_generation",
    "ai_tests",
    "docker_build",
    "kubernetes_rollout",
    "smoke_test",
    "analysis",
    "other",
}
_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret|password|token|authorization)\s*([:=])\s*([^\s,;]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")


def redact_text(value: str, limit: int = 2000) -> str:
    """Redact common credential assignments before optional content capture."""
    text = _SECRET_PATTERN.sub(r"\1\2[REDACTED]", value)
    text = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    return text[:limit]


def langfuse_status() -> dict[str, Any]:
    """Report configuration state without claiming an unverified connection."""
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return {"status": "disabled", "message": "Langfuse credentials are not configured."}
    try:
        import langfuse  # noqa: F401
    except ImportError:
        return {"status": "error", "message": "Langfuse is configured but its SDK is not installed."}
    return {"status": "configured_unverified", "message": "Credentials are configured; endpoint connectivity has not been checked."}


def metrics_status() -> dict[str, str]:
    if PROMETHEUS_AVAILABLE:
        return {"status": "available", "message": "Prometheus metrics are exposed at /metrics."}
    return {"status": "unavailable", "message": "Install the prometheus-client dependency to enable /metrics."}


def metrics_asgi_app():
    if not PROMETHEUS_AVAILABLE:
        return None
    return make_asgi_app(registry=REGISTRY)


def record_http_request(method: str, route: str, status: int, seconds: float) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    allowed_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
    safe_method = method if method in allowed_methods else "OTHER"
    safe_route = route if route.startswith("/") and len(route) < 160 else "unmatched"
    HTTP_REQUESTS.labels(safe_method, safe_route, str(status)).inc()
    HTTP_DURATION.labels(safe_method, safe_route).observe(max(0.0, seconds))


@contextmanager
def measure_operation(operation: str) -> Iterator[None]:
    """Record a bounded operation duration without changing its exceptions."""
    if not PROMETHEUS_AVAILABLE:
        yield
        return
    safe_operation = operation if operation in _SAFE_OPERATIONS else "other"
    ACTIVE_OPERATIONS.labels(safe_operation).inc()
    started = time.perf_counter()
    outcome = "success"
    try:
        yield
    except BaseException:
        outcome = "failure"
        raise
    finally:
        duration = max(0.0, time.perf_counter() - started)
        ACTIVE_OPERATIONS.labels(safe_operation).dec()
        OPERATIONS.labels(safe_operation, outcome).inc()
        OPERATION_DURATION.labels(safe_operation, outcome).observe(duration)


def measured(operation: str):
    """Decorate a sync operation while preserving its exception and return value."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with measure_operation(operation):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def record_llm_request(provider: str, outcome: str, seconds: float) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    provider_label = provider if provider in {"OpenAI", "Groq", "xAI", "none"} else "other"
    outcome_label = outcome if outcome in {"success", "failure"} else "failure"
    LLM_REQUESTS.labels(provider_label, outcome_label).inc()
    LLM_DURATION.labels(provider_label).observe(max(0.0, seconds))


@contextmanager
def trace_observation(
    name: str,
    *,
    as_type: str = "span",
    metadata: dict[str, Any] | None = None,
    input_value: Any = None,
    model: str | None = None,
) -> Iterator[Any | None]:
    """Create a Langfuse observation when configured; tracing cannot break work."""
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        yield None
        return

    try:
        from langfuse import get_client

        client = get_client()
        safe_metadata = dict(metadata or {})
        safe_metadata["content_capture"] = bool(settings.langfuse_capture_content)
        kwargs: dict[str, Any] = {"as_type": as_type, "name": name, "metadata": safe_metadata}
        if model:
            kwargs["model"] = model
        if settings.langfuse_capture_content and input_value is not None:
            if isinstance(input_value, str):
                kwargs["input"] = redact_text(input_value)
            else:
                kwargs["input"] = input_value
        observation_context = client.start_as_current_observation(**kwargs)
        observation = observation_context.__enter__()
    except Exception as exc:
        logger.warning("Langfuse observation unavailable (%s)", type(exc).__name__)
        yield None
        return

    try:
        yield observation
    except BaseException as exc:
        try:
            observation.update(level="ERROR", status_message=type(exc).__name__)
        except Exception as trace_exc:
            logger.warning("Could not update Langfuse error status (%s)", type(trace_exc).__name__)
        try:
            observation_context.__exit__(type(exc), exc, exc.__traceback__)
        except Exception as trace_exc:
            logger.warning("Could not close Langfuse observation (%s)", type(trace_exc).__name__)
        raise
    else:
        try:
            observation_context.__exit__(None, None, None)
        except Exception as exc:
            logger.warning("Could not close Langfuse observation (%s)", type(exc).__name__)


def update_observation(observation: Any | None, **values: Any) -> None:
    if observation is None:
        return
    try:
        if values.get("usage_details") is None:
            values.pop("usage_details", None)
        if "output" in values and not settings.langfuse_capture_content:
            values.pop("output")
        if "input" in values and not settings.langfuse_capture_content:
            values.pop("input")
        if "output" in values and isinstance(values["output"], str):
            values["output"] = redact_text(values["output"])
        observation.update(**values)
    except Exception as exc:
        logger.warning("Could not update Langfuse observation (%s)", type(exc).__name__)
