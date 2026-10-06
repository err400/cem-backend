"""Pipeline diagnostics inherit LOG_LEVEL and the legacy DEBUG alias."""
import json
import logging
import os

DEBUG = os.getenv("DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
DEBUG = DEBUG or os.getenv("LOG_LEVEL", "info").strip().lower() == "debug"
_logger = logging.getLogger("cem.pipeline")
_logger.setLevel(logging.DEBUG if DEBUG else (logging.ERROR if os.getenv("LOG_LEVEL", "info").strip().lower() == "error" else logging.INFO))
_logger.propagate = False
if not _logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s"))
    _logger.addHandler(handler)


def debug(event: str, **fields) -> None:
    if DEBUG:
        _logger.debug("%s %s", event, json.dumps(fields, default=str, sort_keys=True))
