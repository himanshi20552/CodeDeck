"""Environment filtering for commands that execute repository supplied code."""
from __future__ import annotations

import os
import re

_SECRET_NAME = re.compile(r"(?:^|_)(?:API_KEY|TOKEN|PASSWORD|SECRET|CREDENTIALS?|PRIVATE_KEY|ACCESS_KEY)$", re.IGNORECASE)
_SENSITIVE_PARTS = ("SECRET", "PASSWORD", "TOKEN", "CREDENTIAL", "PRIVATE_KEY", "ACCESS_KEY")


def is_sensitive_environment_key(name: str) -> bool:
    upper = name.upper()
    return (upper in {"KUBECONFIG", "DATABASE_URL", "SQLALCHEMY_DATABASE_URI", "DB_URL"}
            or any(part in upper for part in _SENSITIVE_PARTS)
            or upper.endswith("_ACCESS_KEY_ID") or bool(_SECRET_NAME.search(upper)))


def repository_child_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Copy ordinary runtime settings without passing host credentials to repo code."""
    source = os.environ if base is None else base
    return {name: value for name, value in source.items() if not is_sensitive_environment_key(name)}
