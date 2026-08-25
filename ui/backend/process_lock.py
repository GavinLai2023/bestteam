"""Single-instance startup lock over the per-deployment SQLite database.

The backend is single-process by design: `RunRegistry` is in-memory, the
email poller and its overlap guards are per-process (`_dispatch_lock`), and
`runtime.fail_interrupted_runs` releases every outstanding inbox claim at
startup on the assumption that no other process is working them. A second
backend process against the same database -- `uvicorn --workers N`, or a
second container/replica -- would therefore run two pollers, create duplicate
drafts, and release claims the first process still owns.

`acquire_single_instance_lock` turns that misconfiguration into a refusal at
startup: it takes a non-blocking exclusive OS lock on `<db>.lock` next to the
database file and holds it for the process lifetime (the OS releases it on
any exit, clean or killed, so a crashed process never wedges the next start).
`main._lifespan` acquires it before the startup sweeps. `":memory:"` needs no
lock -- an in-memory database is per-process by construction (tests).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class SingleInstanceError(RuntimeError):
    """Another backend process already holds the database lock."""


class _InstanceLock:
    def __init__(self, fd: int, path: Path) -> None:
        self._fd: Optional[int] = fd
        self.path = path

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def acquire_single_instance_lock(db_path: Union[str, Path]) -> Optional[_InstanceLock]:
    if str(db_path) == ":memory:":
        return None
    lock_path = Path(f"{db_path}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise SingleInstanceError(
            f"another backend process already holds {lock_path} -- this deployment "
            "is single-process by design (in-memory run state, one mailbox poller, "
            "a startup sweep that releases every outstanding claim). Do not use "
            "`uvicorn --workers N` or start a second replica against the same "
            "database; stop the other process first."
        ) from None
    return _InstanceLock(fd, lock_path)
