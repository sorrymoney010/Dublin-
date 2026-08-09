"""Strictly monotonic nonce generation for Kraken private endpoints.

Kraken rejects any private request whose nonce is not strictly greater than the
previous nonce seen for that API key.  Two failure modes must be prevented:

1. **Collisions within a process.** ``int(time.time() * 1000)`` repeats when two
   calls land in the same millisecond, so we track the last issued value and
   always advance by at least one.

2. **Regression across restarts.** If the process restarts and the clock has
   drifted backwards (NTP correction, sleep/wake on a laptop), a fresh
   time-derived nonce can be *lower* than one already used, permanently locking
   the key out until the counter catches up.  We persist the high-water mark to
   disk and resume above it.

Nonces are microsecond-resolution to leave headroom for bursts.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path


class NonceGenerator:
    """Thread-safe, restart-safe, strictly increasing nonce source."""

    def __init__(self, state_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._state_path = Path(state_path) if state_path else None
        self._last = self._load_persisted()

    def _load_persisted(self) -> int:
        if self._state_path is None or not self._state_path.exists():
            return 0
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return int(data.get("last_nonce", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            # A corrupt file must not brick the bot; time-based nonces will
            # still be far above zero in practice.
            return 0

    def _persist(self, value: int) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self._state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"last_nonce": value}, handle)
            os.replace(tmp_name, self._state_path)
        except OSError:
            # Persistence is best-effort; in-process monotonicity still holds.
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def next(self) -> int:
        """Return a nonce strictly greater than every nonce previously issued."""
        with self._lock:
            candidate = int(time.time() * 1_000_000)
            if candidate <= self._last:
                candidate = self._last + 1
            self._last = candidate
            self._persist(candidate)
            return candidate

    @property
    def last(self) -> int:
        return self._last
