"""Filesystem-safe validation for client-supplied path components.

`project`, `spot`, and `job_id` are joined into on-disk paths. They must each be
a single, well-behaved path component that can never traverse out of the data
root. ``safe_component`` enforces a strict allowlist; ``ensure_within`` is the
belt-and-suspenders check that a built path really stays under its base.
"""
import re
from pathlib import Path

# A single path component: starts with an alphanumeric or underscore (so no
# leading dot and no leading hyphen), then alphanumerics / underscore / hyphen /
# dot. No separators, no drive letters, no "..". Capped at 64 characters.
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


class UnsafeComponent(ValueError):
    """Raised when a client-supplied path component fails validation."""


def safe_component(value, field: str = "value") -> str:
    if not isinstance(value, str) or not _COMPONENT_RE.match(value) or ".." in value:
        raise UnsafeComponent(
            f"Invalid {field} {value!r}. Use letters, digits, '_', '-', '.' "
            "(no separators, no leading dot, no '..', max 64 chars)."
        )
    return value


def ensure_within(base: Path, target: Path) -> Path:
    """Assert ``target`` resolves inside ``base``; return the resolved target."""
    base_r = base.resolve()
    target_r = target.resolve()
    if not target_r.is_relative_to(base_r):
        raise UnsafeComponent(f"Path escapes allowed directory: {target}")
    return target_r
