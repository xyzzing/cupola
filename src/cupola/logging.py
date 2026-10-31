"""Private operational logging for CUPOLA.

Logs exclude prompts, generated code completions, and reasoning tags.
Only operational metrics, error traces, and backend diagnostic outputs.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class LogEntry:
    """A single operational log entry."""
    ts: float
    level: str
    event: str
    details: dict[str, Any] = None

    def __post_init__(self):
        if self.details is None:
            self.details = {}

    def to_json(self) -> str:
        return json.dumps(asdict(self))


class OperationalLogger:
    """Logger that records only operational events, never content."""

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_path = log_dir / "cupola.log"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        # Set file permissions to 600 (private)
        try:
            self.log_path.chmod(0o600)
        except OSError:
            pass

    def log(self, event: str, level: str = "INFO", **details) -> None:
        """Record an operational event."""
        entry = LogEntry(
            ts=time.time(),
            level=level,
            event=event,
            details=details,
        )
        with self._lock:
            with open(self.log_path, "a") as f:
                f.write(entry.to_json() + "\n")

    def close(self) -> None:
        """Close the logger (no-op for file logger, but provides cleanup hook)."""
        pass

    def info(self, event: str, **details) -> None:
        self.log(event, level="INFO", **details)

    def warn(self, event: str, **details) -> None:
        self.log(event, level="WARN", **details)

    def error(self, event: str, **details) -> None:
        self.log(event, level="ERROR", **details)