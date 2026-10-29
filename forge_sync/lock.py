"""Atomic file lock for preventing concurrent multi-agent git mirror operations.

Writes JSON payload containing PID, hostname, and ISO-8601 timestamp.
Supports automatic expiration of stale locks.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class LockTimeoutError(Exception):
    """Raised when acquiring a lock exceeds the specified timeout."""


class StaleLockError(Exception):
    """Raised when an unrecoverable stale lock condition occurs."""


@dataclass
class LockMetadata:
    pid: int
    hostname: str
    acquired_at: float
    reason: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "pid": self.pid,
                "hostname": self.hostname,
                "acquired_at": self.acquired_at,
                "reason": self.reason,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, payload: str) -> LockMetadata:
        data = json.loads(payload)
        return cls(
            pid=int(data.get("pid", 0)),
            hostname=str(data.get("hostname", "")),
            acquired_at=float(data.get("acquired_at", 0.0)),
            reason=str(data.get("reason", "")),
        )


class FileLock:
    """Atomic file-based mutual exclusion lock."""

    def __init__(
        self,
        lock_path: Path | str,
        timeout: float = 30.0,
        stale_threshold: float = 60.0,
        poll_interval: float = 0.2,
    ) -> None:
        self.lock_path = Path(lock_path).resolve()
        self.timeout = timeout
        self.stale_threshold = stale_threshold
        self.poll_interval = poll_interval
        self._acquired = False

    @property
    def is_locked(self) -> bool:
        return self.lock_path.exists()

    def read_metadata(self) -> Optional[LockMetadata]:
        if not self.lock_path.exists():
            return None
        try:
            return LockMetadata.from_json(self.lock_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _is_stale(self, metadata: Optional[LockMetadata]) -> bool:
        if not metadata or metadata.acquired_at <= 0:
            return True
        age = time.time() - metadata.acquired_at
        return age > self.stale_threshold

    def acquire(self, reason: str = "") -> None:
        """Acquire the lock, waiting up to timeout seconds."""
        deadline = time.time() + self.timeout
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        meta = LockMetadata(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            acquired_at=time.time(),
            reason=reason,
        )

        while True:
            try:
                # O_CREAT | O_EXCL ensures atomic creation
                fd = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(meta.to_json())
                self._acquired = True
                return
            except FileExistsError:
                # Check for stale lock
                existing = self.read_metadata()
                if self._is_stale(existing):
                    try:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                    except OSError:
                        pass

                if time.time() >= deadline:
                    holder_info = f"held by PID {existing.pid} on {existing.hostname}" if existing else "held"
                    raise LockTimeoutError(
                        f"Timed out after {self.timeout:.1f}s waiting for lock {self.lock_path} ({holder_info})"
                    )

                time.sleep(self.poll_interval)

    def release(self) -> None:
        """Release the acquired lock."""
        if not self._acquired:
            return

        try:
            meta = self.read_metadata()
            # Only remove if we own it (matching PID and hostname)
            if meta and meta.pid == os.getpid() and meta.hostname == socket.gethostname():
                self.lock_path.unlink(missing_ok=True)
            elif not meta:
                self.lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            self._acquired = False

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
