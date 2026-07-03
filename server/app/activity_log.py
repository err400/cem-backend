"""Activity log: one JSONL line per user action.

    <LOG_DIR>/activity.jsonl        current file (grows to ~10MB)
    <LOG_DIR>/activity.jsonl.1..N   size-rotated backups

Each line records who did what (user identity from request headers) plus enough
context to find the run's own logs under data/jobs/<job_id>/. The `service` field
keeps the format shared, so other backends (e.g. drone) can write the same shape.
"""
import json
import logging
import shutil
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Optional

from .settings import get_settings

_SERVICE = "cem-backend"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 10

_LOCK = Lock()
_LOGGER: Optional[logging.Logger] = None


def _logger() -> logging.Logger:
    global _LOGGER
    if _LOGGER is None:
        with _LOCK:
            if _LOGGER is None:
                log_dir = get_settings().LOG_DIR
                log_dir.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(
                    log_dir / "activity.jsonl",
                    maxBytes=_MAX_BYTES,
                    backupCount=_BACKUP_COUNT,
                    encoding="utf-8",
                )
                handler.setFormatter(logging.Formatter("%(message)s"))
                logger = logging.getLogger("cem.activity")
                logger.setLevel(logging.INFO)
                logger.propagate = False
                logger.addHandler(handler)
                _LOGGER = logger
    return _LOGGER


def append(user: dict, action: str, **fields) -> None:
    user = user or {}
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "service": _SERVICE,
        "user_email": user.get("email"),
        "user_id": user.get("id"),
        "action": action,
    }
    entry.update({k: v for k, v in fields.items() if v is not None})
    _logger().info(json.dumps(entry, ensure_ascii=False))


def copy_run_log(src: Path, job_id: str, step: str) -> Optional[str]:
    """Duplicate a run's own log into the logging directory so the logs dir is
    self-contained (activity ledger + the actual run output). Returns the path
    relative to LOG_DIR, or None if there was nothing to copy."""
    if not src.is_file():
        return None
    log_dir = get_settings().LOG_DIR
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest_dir = log_dir / "runs" / day
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{job_id}_{step}.log"
    try:
        shutil.copyfile(src, dest)
    except OSError:
        return None
    return str(dest.relative_to(log_dir))
