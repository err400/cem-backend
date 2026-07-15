"""
Thin REST client for the sibling FileBrowser service (optional public share
links for job result folders). Callers (jobs.py, runner.py, retention.py)
already wrap every call in try/except and treat failures as best-effort, so
this module lets errors propagate rather than swallowing them itself.

FileBrowser API (v2, github.com/filebrowser/filebrowser):
  POST /api/login             -> raw JWT string body (not JSON)
  POST   /api/share/<path>    -> create a share, returns {hash, path, expire, ...}
  DELETE /api/share/<hash>    -> revoke a share by hash
Auth: the JWT goes in the X-Auth header on every request after login.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from .settings import get_settings

_TOKEN_TTL = 300  # seconds; re-login well before the JWT's own expiry
_lock = threading.Lock()
_token: str | None = None
_token_at: float = 0.0


def is_configured() -> bool:
    return get_settings().filebrowser_enabled


def _login() -> str:
    s = get_settings()
    body = json.dumps({
        "username": s.FILEBROWSER_USERNAME,
        "password": s.FILEBROWSER_PASSWORD,
        "recaptcha": "",
    }).encode()
    req = urllib.request.Request(
        f"{s.FILEBROWSER_BASE_URL}/api/login", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=s.FILEBROWSER_TIMEOUT) as resp:
        return resp.read().decode().strip()


def _token_cached() -> str:
    global _token, _token_at
    with _lock:
        if _token is None or (time.time() - _token_at) > _TOKEN_TTL:
            _token = _login()
            _token_at = time.time()
        return _token


def _request(method: str, rel: str, retry_on_401: bool = True) -> dict | None:
    s = get_settings()
    url = f"{s.FILEBROWSER_BASE_URL}/api/share/{quote(rel.lstrip('/'), safe='/')}"
    req = urllib.request.Request(url, method=method, headers={"X-Auth": _token_cached()})
    try:
        with urllib.request.urlopen(req, timeout=s.FILEBROWSER_TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry_on_401:
            # cached token expired/revoked server-side; force one re-login
            global _token
            with _lock:
                _token = None
            return _request(method, rel, retry_on_401=False)
        raise


def create_share(path: str) -> dict | None:
    """Create a public share link for *path* (relative to DATA_DIR, which is
    the FileBrowser root). Returns the share record (hash, path, expire,
    hasPassword) or None if FileBrowser is not configured."""
    if not is_configured():
        return None
    return _request("POST", path)


def delete_share(hash_: str) -> None:
    """Revoke a share by its hash."""
    if not is_configured():
        return
    _request("DELETE", hash_)
