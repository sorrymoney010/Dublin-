"""Single-instance lock + ledger-owner guard for the paper loop.

* ``InstanceLock``: an exclusive, non-blocking ``fcntl.flock`` on
  ``logs/paper_trader.lock``. The kernel drops it when the process dies, so a
  crash never leaves a stale lock; the file only carries the holder's pid for
  diagnostics. A second loop waits up to ``wait_seconds`` (a ``launchctl
  kickstart -k`` restart overlaps the old process by ~1 s) and then refuses.
* ``ledger_owner_ok``: only the machine whose wrapper exports
  ``MAYO_LEDGER_OWNER=1`` (the Mac, ``scripts/run_paper_mac.sh``) may write
  the paper ledger. Any other copy (e.g. a stale clone on another box) refuses
  to start instead of forking the book.
"""
from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

LEDGER_OWNER_ENV = "MAYO_LEDGER_OWNER"


class InstanceLock:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fh = None

    def holder_pid(self) -> int | None:
        try:
            txt = self.path.read_text(encoding="utf-8").strip()
            return int(txt) if txt else None
        except (OSError, ValueError):
            return None

    def acquire(self, wait_seconds: float = 0.0, poll: float = 0.5) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    fh.close()
                    return False
                time.sleep(poll)
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


def ledger_owner_ok(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(LEDGER_OWNER_ENV, "")).strip() == "1"
