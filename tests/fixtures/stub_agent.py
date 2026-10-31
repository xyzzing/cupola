#!/usr/bin/env python3
"""A stand-in for an agent harness, for testing the benchmark runner.

Substitutes for ``dsh`` in runner tests: it consumes the same argv shape
(a command that receives a kickoff prompt), writes a session log in the dsh
format so token accounting can be exercised, and can be told to solve the
task, ignore it, hang, or overspend.

Environment:

``STUB_MODE``
    ``solve`` (default) writes the fix for the fixture it recognises,
    ``noop`` does nothing, ``hang`` sleeps past its wall budget,
    ``overspend`` writes a session log whose token total exceeds any budget.

``STUB_DSH_HOME``
    Where to write ``sessions/<workspace>/session-<id>/session.v3.jsonl.zstd``
    so the runner's budget watcher finds it. Defaults to a temp directory.

``STUB_OUTPUT``
    Optional text to write to stdout, to exercise log capture.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _session_root() -> Path:
    # The runner exports CUPOLA_DSH_SESSIONS so a harness writes its log where
    # the budget watcher looks; STUB_DSH_HOME is the test-only override.
    explicit = os.environ.get("CUPOLA_DSH_SESSIONS")
    if explicit:
        return Path(explicit)
    root = os.environ.get("STUB_DSH_HOME")
    if root:
        return Path(root) / "sessions"
    return Path(os.environ.get("TMPDIR", "/tmp")) / "stub-dsh" / "sessions"


def _encode(cwd: str) -> str:
    return "--" + cwd.strip("/").replace("/", "-") + "--"


def write_session(cwd: str, output_tokens: int = 10,
                  calls: int = 1) -> Path:
    """Write a session log the transcript parser can read."""
    directory = _session_root() / _encode(cwd) / "session-stub"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "session.v3.jsonl.zstd"
    now_ms = int(time.time() * 1000)

    records = [{"type": "session", "version": 3, "id": "session-stub",
                "cwd": cwd, "createdAt": now_ms}]
    for call in range(calls):
        records.append({
            "type": "assistant/message",
            "seq": call + 1,
            "time": now_ms + call,
            "data": {
                "turn": call + 1,
                "step": call + 1,
                "usage": {"inputTokens": 100, "outputTokens": output_tokens,
                          "cacheReadTokens": 0,
                          "totalTokens": 100 + output_tokens},
                "message": {
                    "id": f"stub-{call}",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "stub"}],
                    "source": {"kind": "model", "model": "stub-model",
                               "provider": "stub"},
                },
            },
        })
    payload = b"\n".join(json.dumps(r).encode() for r in records) + b"\n"

    try:
        import compression.zstd as zstd
        path.write_bytes(zstd.compress(payload))
    except ImportError:  # pragma: no cover - older runtimes
        path = directory / "session.v3.jsonl"
        path.write_bytes(payload)
    return path


def solve(cwd: Path) -> None:
    """Apply the known fix for whichever fixture is present."""
    task = cwd / "tasks" / "keyerror_default" / "task.py"
    if task.is_file():
        task.write_text(
            'SETTINGS = {"retries": 2}\n\n\n'
            'def get_setting(key, settings=None):\n'
            '    settings = settings or SETTINGS\n'
            '    return settings.get(key)\n')
        return
    task = cwd / "tasks" / "assertion_expectation" / "task.py"
    if task.is_file():
        task.write_text(
            'def totals(items):\n'
            '    return sum(items)\n')
        return
    # Unknown fixture: write a generic marker rather than guessing.
    (cwd / "stub-solved.txt").write_text("solved\n")


def main(argv: list[str]) -> int:
    mode = os.environ.get("STUB_MODE", "solve")
    cwd = Path.cwd()
    output = os.environ.get("STUB_OUTPUT")
    if output:
        print(output)

    if mode == "hang":
        write_session(str(cwd))
        time.sleep(3600)
        return 0

    if mode == "overspend":
        write_session(str(cwd), output_tokens=10_000_000, calls=200)
        time.sleep(3600)
        return 0

    if mode == "noop":
        write_session(str(cwd))
        return 0

    solve(cwd)
    write_session(str(cwd))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
