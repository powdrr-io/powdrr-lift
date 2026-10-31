"""Portable, process-level exclusive file locks for local persistence."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout


@contextmanager
def file_lock(
    path: str | Path,
    *,
    blocking: bool = True,
    mode: int = 0o600,
) -> Iterator[None]:
    """Lock ``path`` exclusively, failing immediately when ``blocking`` is false.

    FileLock selects the native hard-lock implementation for the current OS. Soft
    lock fallback is disabled so unsupported filesystems fail closed instead of
    silently weakening process exclusion or crash-release behavior.
    """
    lock = FileLock(
        path,
        mode=mode,
        thread_local=False,
        fallback_to_soft=False,
        preserve_lock_file=True,
    )
    lock.acquire(blocking=blocking, timeout=-1 if blocking else 0)
    try:
        yield
    finally:
        lock.release()


__all__ = ["FileLock", "Timeout", "file_lock"]
