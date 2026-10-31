"""Read a dsh session log for authoritative run accounting.

``dsh``'s NDJSON output carries no token or cost fields, so the session log is
the only non-proxy source for what a run actually consumed. The log is
``session.v3.jsonl[.zstd]``: a header line followed by event records.

Two rules:

* Usage is deduplicated by assistant message id. A durable log may replay a
  message, and double-counting it would silently inflate every comparison.
* A corrupt frame is reported, never fatal. A truncated log is still evidence
  for the part that arrived, and a benchmark that dies on a partial log turns
  a usable data point into no data point.

``compression.zstd`` is stdlib from Python 3.14, so no dependency is needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Union

try:  # Python 3.14+
    import compression.zstd as _zstd
except ImportError:  # pragma: no cover - exercised only on older runtimes
    _zstd = None  # type: ignore[assignment]

SESSION_HEADER_TYPE = "session"
SUPPORTED_VERSIONS = (1, 2, 3)


class TranscriptError(Exception):
    """The log cannot be read at all (missing, unsupported, undecompressable)."""


@dataclass
class Usage:
    """Token accounting for a run, deduplicated by assistant message."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
        }


@dataclass
class EngineStats:
    """What the session recorded about the agent engine, not the model output."""

    event_counts: dict = field(default_factory=dict)
    turns: int = 0
    steps: int = 0
    models: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    tool_calls: int = 0
    retries: int = 0
    started_at: Optional[int] = None
    ended_at: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "event_counts": dict(sorted(self.event_counts.items())),
            "turns": self.turns,
            "steps": self.steps,
            "models": list(self.models),
            "providers": list(self.providers),
            "tool_calls": self.tool_calls,
            "retries": self.retries,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    def wall_seconds(self) -> Optional[float]:
        if self.started_at is None or self.ended_at is None:
            return None
        return max(0.0, (self.ended_at - self.started_at) / 1000.0)


@dataclass
class SessionTranscript:
    """A parsed session log."""

    session_id: str = ""
    version: Optional[int] = None
    cwd: str = ""
    origin: str = ""
    created_at: Optional[int] = None
    parent_session: Optional[str] = None
    usage: Usage = field(default_factory=Usage)
    engine: EngineStats = field(default_factory=EngineStats)
    problems: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "version": self.version,
            "cwd": self.cwd,
            "origin": self.origin,
            "created_at": self.created_at,
            "parent_session": self.parent_session,
            "usage": self.usage.to_dict(),
            "engine": self.engine.to_dict(),
            "wall_seconds": self.engine.wall_seconds(),
            "problems": list(self.problems),
        }


def read_lines(path: Union[str, Path]) -> list[bytes]:
    """Return the JSONL lines of a session log, transparently decompressed."""
    path = Path(path)
    if not path.is_file():
        raise TranscriptError(f"no session log at {path}")
    raw = path.read_bytes()
    if path.suffix == ".zstd" or raw[:4] == b"\x28\xb5\x2f\xfd":
        return _decompress(raw, path)
    return raw.splitlines()


def _decompress(raw: bytes, path: Path) -> list[bytes]:
    if _zstd is None:
        raise TranscriptError(
            f"{path} is zstd-compressed and stdlib compression.zstd is "
            "unavailable (needs Python 3.14+; no third-party fallback is "
            "installed by design)")
    try:
        return _zstd.decompress(raw).splitlines()
    except Exception as exc:  # zstd raises its own error types
        raise TranscriptError(f"cannot decompress {path}: {exc}") from exc


def _as_int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_records(lines: Iterable[bytes]) -> SessionTranscript:
    """Parse session JSONL lines into a transcript. Never raises on bad data."""
    transcript = SessionTranscript()

    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            transcript.problems.append(f"line {index}: unparseable JSON")
            continue
        if not isinstance(record, dict):
            transcript.problems.append(f"line {index}: not an object")
            continue

        kind = record.get("type")
        if kind == SESSION_HEADER_TYPE and not transcript.session_id:
            _apply_header(transcript, record)
            continue
        if isinstance(kind, str):
            transcript.engine.event_counts[kind] = \
                transcript.engine.event_counts.get(kind, 0) + 1
            if kind.startswith("llm/retry"):
                transcript.engine.retries += 1
        transcript.events.append(record)
        _apply_event(transcript, record)

    transcript.engine.models = sorted(set(transcript.engine.models))
    transcript.engine.providers = sorted(set(transcript.engine.providers))
    return transcript


def _apply_header(transcript: SessionTranscript, record: dict) -> None:
    transcript.session_id = str(record.get("id") or "")
    version = record.get("version")
    transcript.version = version if isinstance(version, int) else None
    if transcript.version is not None and \
            transcript.version not in SUPPORTED_VERSIONS:
        transcript.problems.append(
            f"unsupported session version {transcript.version}")
    transcript.cwd = str(record.get("cwd") or "")
    transcript.origin = str(record.get("origin") or "")
    created = record.get("createdAt")
    transcript.created_at = created if isinstance(created, int) else None
    parent = record.get("parentSession")
    transcript.parent_session = parent if isinstance(parent, str) else None


#: Seen message ids, held on the transcript to dedupe replayed usage records.
def _seen(transcript: SessionTranscript) -> set:
    seen = getattr(transcript, "_seen_message_ids", None)
    if seen is None:
        seen = set()
        setattr(transcript, "_seen_message_ids", seen)
    return seen


def _apply_event(transcript: SessionTranscript, record: dict) -> None:
    kind = record.get("type")
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    when = record.get("time")
    if isinstance(when, int):
        if transcript.engine.started_at is None:
            transcript.engine.started_at = when
        transcript.engine.ended_at = when

    if kind == "assistant/message":
        message = data.get("message") if isinstance(data.get("message"), dict) \
            else {}
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id:
            if message_id in _seen(transcript):
                return
            _seen(transcript).add(message_id)

        usage = data.get("usage")
        if isinstance(usage, dict):
            transcript.usage.input_tokens += _as_int(usage.get("inputTokens"))
            transcript.usage.output_tokens += _as_int(usage.get("outputTokens"))
            transcript.usage.cache_read_tokens += \
                _as_int(usage.get("cacheReadTokens"))
            total = _as_int(usage.get("totalTokens"))
            if not total:
                total = _as_int(usage.get("inputTokens")) + \
                    _as_int(usage.get("outputTokens"))
            transcript.usage.total_tokens += total
            transcript.usage.calls += 1

        _collect_identity(transcript, message, data)
        _count_tool_calls(transcript, message)
        transcript.engine.steps = max(transcript.engine.steps,
                                      _as_int(data.get("step")))
        transcript.engine.turns = max(transcript.engine.turns,
                                      _as_int(data.get("turn")))
        return

    if kind in ("turn/start", "turn/end"):
        transcript.engine.turns = max(transcript.engine.turns,
                                      _as_int(data.get("turn")))
    elif kind in ("step/start", "step/end"):
        transcript.engine.steps = max(transcript.engine.steps,
                                      _as_int(data.get("step")))


def _collect_identity(transcript: SessionTranscript, message: dict,
                      data: dict) -> None:
    source = message.get("source")
    if isinstance(source, dict):
        if isinstance(source.get("model"), str):
            transcript.engine.models.append(source["model"])
        if isinstance(source.get("provider"), str):
            transcript.engine.providers.append(source["provider"])
        return
    # Direct-model/system records carry identity on the event itself.
    for key, bucket in (("model", transcript.engine.models),
                        ("provider", transcript.engine.providers)):
        value = data.get(key)
        if isinstance(value, str) and value:
            bucket.append(value)


def _count_tool_calls(transcript: SessionTranscript, message: dict) -> None:
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool-call":
            transcript.engine.tool_calls += 1


def parse_session(path: Union[str, Path]) -> SessionTranscript:
    """Parse a session log file (``.zstd`` or plain JSONL)."""
    return parse_records(read_lines(path))


def parse_bytes(payload: bytes) -> SessionTranscript:
    """Parse in-memory log bytes. Used by tests and the runner's tail reader."""
    return parse_records(payload.splitlines())


#: A log may be compressed or plain. dsh writes zstd, but a harness on a
#: runtime without stdlib zstd (Python < 3.14) writes plain JSONL, and a
#: watcher that only looked for one form would silently account zero tokens.
SESSION_LOG_PATTERNS = ("*/*/session.v3.jsonl.zstd", "*/*/session.v3.jsonl")


def find_session_logs(sessions_root: Union[str, Path],
                      cwd: Optional[str] = None) -> list[Path]:
    """Locate session logs, optionally for one working directory.

    dsh encodes the session cwd in the directory name (``--a-b--`` for
    ``/a/b``), which is how a run's own log is found without guessing.
    """
    root = Path(sessions_root)
    if not root.is_dir():
        return []
    found: set[Path] = set()
    for pattern in SESSION_LOG_PATTERNS:
        found.update(root.glob(pattern))
    logs = sorted(found)
    if cwd is not None:
        encoded = "--" + cwd.strip("/").replace("/", "-") + "--"
        logs = [path for path in logs if path.parts[-3].startswith(encoded)]
    return logs


def newest_session_log(sessions_root: Union[str, Path],
                       cwd: Optional[str] = None,
                       started_after: Optional[float] = None) -> Optional[Path]:
    """The most recent log, optionally only one modified after a timestamp.

    ``started_after`` is what binds a log to a run: a benchmark that picks up
    a stale session would attribute another run's tokens to this one.
    """
    candidates = find_session_logs(sessions_root, cwd)
    if started_after is not None:
        candidates = [path for path in candidates
                      if path.stat().st_mtime >= started_after]
    if not candidates:
        return None
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    # Same session recorded twice: prefer the compressed copy.
    if newest.suffix != ".zstd":
        compressed = newest.with_name(newest.name + ".zstd")
        if compressed.is_file():
            return compressed
    return newest
