"""Per-job advisory run lock.

Serialises execution of the same job so a duplicate or retried run cannot race
the first one on the shared ``work/`` outputs. The lock is NON-BLOCKING: a second
concurrent run of the same job fails fast with ``JobBusy`` so the API can answer
409 instead of two subprocesses interleaving writes into ``work/aggregate.csv``.

OS advisory locks are used (``fcntl`` on POSIX, ``msvcrt`` on Windows). They are
released automatically when the holding process dies, so a crash can never wedge
a job permanently the way a plain lock-file sentinel would.
"""
import os
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl  # POSIX
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    import msvcrt
    _HAVE_FCNTL = False


class JobBusy(Exception):
    """Raised when a job's run lock is already held (a run is in flight)."""


def _try_lock(fd: int) -> bool:
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def job_run_lock(lock_dir: Path, job_id: str):
    """Hold an exclusive advisory lock for ``job_id``; raise ``JobBusy`` if held."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_dir / f"{job_id}.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if not _try_lock(fd):
            raise JobBusy(job_id)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
