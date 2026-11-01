"""Per-operation deployment target override for optional local kind runs."""
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

from app.core.config import settings

_target_override: ContextVar[str | None] = ContextVar("deployment_target_override", default=None)


def deployment_target() -> str:
    return _target_override.get() or settings.deployment_target


@contextmanager
def use_deployment_target(target: str) -> Iterator[None]:
    token = _target_override.set(target)
    try:
        yield
    finally:
        _target_override.reset(token)
