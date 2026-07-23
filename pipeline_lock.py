"""Small cross-platform single-run lock for the daily pipeline."""

from __future__ import annotations

import os
import time
from pathlib import Path

import config


class PipelineAlreadyRunningError(RuntimeError):
    pass


class PipelineRunLock:
    def __init__(self, path: str | None = None, stale_hours: int | None = None):
        self.path = Path(path or config.PIPELINE_LOCK_FILE)
        self.stale_seconds = max(
            3600,
            int(stale_hours or config.PIPELINE_LOCK_STALE_HOURS) * 3600,
        )
        self._acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > self.stale_seconds:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise PipelineAlreadyRunningError(
                    f"Another pipeline run holds {self.path}"
                )
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(f"pid={os.getpid()}\nstarted_at={time.time()}\n")
                self._acquired = True
                return
        raise PipelineAlreadyRunningError(f"Could not acquire {self.path}")

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.release()
        return False
