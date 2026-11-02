import re
import tomllib
from pathlib import Path

SUSPICIOUS_REQUIREMENT_NAMES = {"install"}
LEGACY_PYTHON_310_PACKAGES = {
    "greenlet": (2, 0),
    "httptools": (0, 5),
    "orjson": (3, 8),
}


def _requirement_name(line: str) -> str | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith(("-", "git+", "http://", "https://")):
        return None
    text = text.split(";", 1)[0].strip()
    text = re.split(r"[<>=!~]", text, maxsplit=1)[0].strip()
    text = text.split("[", 1)[0].strip()
    if not text:
        return None
    return text.lower()


def sanitize_requirements_file(repo_dir: Path) -> list[str]:
    path = repo_dir / "requirements.txt"
    if not path.is_file():
        return []

    original = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    kept: list[str] = []
    removed: list[str] = []
    for line in original:
        name = _requirement_name(line)
        if name in SUSPICIOUS_REQUIREMENT_NAMES:
            removed.append(line.strip())
            continue
        kept.append(line)

    if removed:
        content = "\n".join(kept).rstrip()
        path.write_text((content + "\n") if content else "", encoding="utf-8")
    return removed


def _parse_requirement_version(line: str) -> tuple[str | None, tuple[int, ...] | None]:
    text = line.strip()
    if not text or text.startswith("#"):
        return None, None
    match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*==\s*([0-9][0-9A-Za-z_.-]*)", text)
    if not match:
        return None, None
    name = match.group(1).lower()
    version_parts: list[int] = []
    for part in match.group(2).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            break
        version_parts.append(int(digits))
    return name, tuple(version_parts) if version_parts else None


def choose_python_base_image(repo_dir: Path, default_tag: str = "3.11-slim") -> str:
    # Old lockfiles can pin native extensions that predate Python 3.11 even
    # when the project has no requirements.txt (for example Poetry projects
    # pinning greenlet 1.x). Use the same compatibility rule for requirements
    # and Poetry's package/version entries so generated staging images match
    # the dependency set the repository actually locked.
    legacy_python_310 = False
    requirements = repo_dir / "requirements.txt"
    if requirements.is_file():
        for line in requirements.read_text(encoding="utf-8", errors="ignore").splitlines():
            name, version = _parse_requirement_version(line)
            threshold = LEGACY_PYTHON_310_PACKAGES.get(name or "")
            if threshold and version and version < threshold:
                legacy_python_310 = True
                break

    poetry_lock = repo_dir / "poetry.lock"
    if poetry_lock.is_file():
        package_name: str | None = None
        for line in poetry_lock.read_text(encoding="utf-8", errors="ignore").splitlines():
            name_match = re.match(r'^name\s*=\s*"([A-Za-z0-9_.-]+)"\s*$', line)
            if name_match:
                package_name = name_match.group(1).lower().replace("_", "-")
                continue
            version_match = re.match(r'^version\s*=\s*"([0-9][0-9A-Za-z_.-]*)"\s*$', line)
            if version_match and package_name:
                version = _parse_requirement_version(f"{package_name}=={version_match.group(1)}")[1]
                threshold = LEGACY_PYTHON_310_PACKAGES.get(package_name)
                if threshold and version and version < threshold:
                    legacy_python_310 = True
                    break
                package_name = None

    default_match = re.match(r"^(\d+)\.(\d+)", default_tag)
    default_version = (
        (int(default_match.group(1)), int(default_match.group(2)))
        if default_match else (3, 11)
    )
    minimum_version: tuple[int, int] | None = None
    pyproject = repo_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            project = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="ignore")).get("project", {})
            requires_python = project.get("requires-python", "")
            # PEP 621 commonly expresses the lower supported runtime as
            # ``>=3.14`` or ``~=3.14``. Keep the default for older/equal
            # minima, but never generate an image below the declared minimum.
            lower_bounds = re.findall(r"(?:>=|~=)\s*(\d+)\.(\d+)", requires_python)
            if lower_bounds:
                minimum_version = max((int(major), int(minor)) for major, minor in lower_bounds)
        except (OSError, tomllib.TOMLDecodeError, AttributeError, TypeError):
            minimum_version = None

    if legacy_python_310 and (minimum_version is None or minimum_version <= (3, 10)):
        return "python:3.10-slim"
    selected = max(default_version, minimum_version or default_version)
    return f"python:{selected[0]}.{selected[1]}-slim"
