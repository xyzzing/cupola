"""CUPOLA state management with atomic writes."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional


@dataclass
class StateRecord:
    """Persistent state for the CUPOLA supervisor."""
    state: str = "STOPPED"  # STOPPED, VALIDATING, STARTING, READY, STOPPING, FAILED, DEGRADED
    backend_pid: Optional[int] = None
    backend_start_time: Optional[float] = None
    profile_alias: Optional[str] = None
    profile_hash: Optional[str] = None
    instance_id: Optional[str] = None
    last_error: Optional[str] = None


class AtomicFile:
    """Atomic file writer: writes to temp file, then renames."""

    def __init__(self, path: Path):
        self.path = path

    def write(self, data: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def load_state(state_dir_or_path) -> StateRecord:
    """Load state from disk. Returns default if not found.
    
    Accepts either a directory path (will look for state.json inside)
    or a direct file path to the state.json file.
    """
    if isinstance(state_dir_or_path, str):
        state_dir_or_path = Path(state_dir_or_path)
    
    # If it's a file, use it directly; if it's a directory, look for state.json
    if state_dir_or_path.is_file():
        state_path = state_dir_or_path
    else:
        state_path = state_dir_or_path / "state.json"
    
    if not state_path.exists():
        return StateRecord()
    
    # Reject symlinks for security
    if state_path.is_symlink():
        raise ValueError(f"State file is a symlink: {state_path}")
    
    try:
        with open(state_path) as f:
            data = json.load(f)
        # Filter to known fields to handle schema evolution gracefully
        known_fields = {f.name for f in fields(StateRecord)}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return StateRecord(**filtered)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return StateRecord()


def save_state(state_dir: Path, record: StateRecord) -> None:
    """Atomically save state to disk."""
    state_path = state_dir / "state.json"
    atomic = AtomicFile(state_path)
    atomic.write(json.dumps(asdict(record), indent=2) + "\n")