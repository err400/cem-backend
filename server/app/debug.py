"""Level-based compute logging and streaming-safe API request diagnostics."""
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import perf_counter
from uuid import uuid4

DEBUG = os.getenv("DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
_level = os.getenv("LOG_LEVEL", "info").strip().lower()
DEBUG = DEBUG or _level == "debug"
LOG_LEVEL = logging.DEBUG if DEBUG else (logging.ERROR if _level == "error" else logging.INFO)
_logger = logging.getLogger("cem.compute")
_logger.setLevel(LOG_LEVEL)
_logger.propagate = False
# Replace our handlers when reloaded, rather than duplicating output.
for handler in list(_logger.handlers):
    _logger.removeHandler(handler)
    handler.close()
_formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
_stream = logging.StreamHandler()
_stream.setFormatter(_formatter)
_logger.addHandler(_stream)
try:
    _log_dir = Path(os.getenv("LOG_DIR", "/logs"))
    _log_dir.mkdir(parents=True, exist_ok=True)
    _file = RotatingFileHandler(_log_dir / "app.log", maxBytes=10 * 1024 * 1024,
                                backupCount=5, encoding="utf-8")
    _file.setFormatter(_formatter)
    _logger.addHandler(_file)
except OSError:
    _logger.error("logging.file_unavailable: check LOG_DIR permissions; stdout logging remains active")


def _emit(level: int, event: str, fields: dict) -> None:
    if _logger.isEnabledFor(level):
        _logger.log(level, "%s %s", event, json.dumps(fields, default=str, sort_keys=True))


def debug(event: str, **fields) -> None:
    if DEBUG:
        _emit(logging.DEBUG, event, fields)


def info(event: str, **fields) -> None:
    _emit(logging.INFO, event, fields)


def error(event: str, **fields) -> None:
    _emit(logging.ERROR, event, fields)


class DebugRequests:
    """Log request summaries without consuming or buffering request/audio bodies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid4().hex[:12]
        started = perf_counter()
        status = 500
        failed = False
        debug("http.start", request_id=request_id, method=scope["method"])

        async def observe(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, observe)
        except Exception as exc:
            failed = True
            error("http.error", request_id=request_id, error=type(exc).__name__)
            raise
        finally:
            route = getattr(scope.get("route"), "path", "<unmatched>")
            log = error if failed or status >= 400 else info
            log("http.finish", request_id=request_id, method=scope["method"],
                route=route, status=status,
                elapsed_ms=round((perf_counter() - started) * 1000, 1))
