"""Tests for dsh session-log accounting.

Usage must be deduplicated by message id (a durable log can replay a message),
and a corrupt or truncated log must degrade to partial evidence rather than
aborting the benchmark that is consuming it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cupola.transcript import (
    TranscriptError,
    find_session_logs,
    newest_session_log,
    parse_bytes,
    parse_records,
    parse_session,
    read_lines,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "transcript"
#: The same session is shipped compressed and plain. compression.zstd is
#: stdlib only from Python 3.14, and CI runs 3.11-3.14, so tests resolve
#: whichever form this interpreter can read rather than assuming one.
COMPRESSED = FIXTURE_DIR / "session-minimal.v3.jsonl.zstd"
PLAIN = FIXTURE_DIR / "session-minimal.v3.jsonl"

try:  # noqa: SIM105 - explicit probe keeps the reason visible
    import compression.zstd  # noqa: F401
    HAS_ZSTD = True
except ImportError:  # pragma: no cover - exercised on Python < 3.14
    HAS_ZSTD = False

FIXTURE = COMPRESSED if HAS_ZSTD else PLAIN

requires_zstd = pytest.mark.skipif(
    not HAS_ZSTD,
    reason="session logs are zstd-compressed and compression.zstd needs "
           "Python 3.14+; the plain fixture covers the other runtimes")


def _session_lines(events: list[dict], header: dict | None = None) -> bytes:
    records = [header or {"type": "session", "version": 3, "id": "s1",
                          "cwd": "/ws", "createdAt": 1000}]
    records.extend(events)
    return b"\n".join(json.dumps(r).encode() for r in records) + b"\n"


def _assistant(message_id: str, usage: dict, model: str = "m",
               turn: int = 1, step: int = 1, tools: int = 0) -> dict:
    content = [{"type": "tool-call", "id": f"c{i}", "name": "bash"}
               for i in range(tools)]
    return {
        "type": "assistant/message",
        "seq": 1,
        "time": 1500,
        "data": {
            "turn": turn,
            "step": step,
            "usage": usage,
            "message": {"id": message_id, "role": "assistant",
                        "content": content,
                        "source": {"kind": "model", "model": model,
                                   "provider": "local"}},
        },
    }


# --- fixture --------------------------------------------------------------


def test_parse_checked_in_session_fixture():
    transcript = parse_session(FIXTURE)
    assert transcript.session_id == "session-fixture-0001"
    assert transcript.version == 3
    assert transcript.problems == []
    assert transcript.usage.calls == 1
    assert transcript.usage.total_tokens > 0
    assert transcript.usage.cache_read_tokens > 0
    assert transcript.engine.turns == 1
    assert transcript.engine.wall_seconds() is not None


def test_fixture_carries_no_private_paths():
    text = Path(PLAIN).read_bytes()
    assert b"/home/" not in text
    assert b"/TMP-WORKSPACE" in text


# --- accounting -----------------------------------------------------------


def test_usage_totals_across_messages():
    payload = _session_lines([
        _assistant("a", {"inputTokens": 10, "outputTokens": 5,
                         "totalTokens": 15, "cacheReadTokens": 1}),
        _assistant("b", {"inputTokens": 20, "outputTokens": 7,
                         "totalTokens": 27, "cacheReadTokens": 2},
                   turn=2, step=2, tools=2),
    ])
    transcript = parse_bytes(payload)
    assert transcript.usage.input_tokens == 30
    assert transcript.usage.output_tokens == 12
    assert transcript.usage.total_tokens == 42
    assert transcript.usage.cache_read_tokens == 3
    assert transcript.usage.calls == 2
    assert transcript.engine.tool_calls == 2
    assert transcript.engine.turns == 2
    assert transcript.engine.steps == 2


def test_replayed_message_is_not_double_counted():
    """A durable log can repeat a message; usage must count it once."""
    record = _assistant("same-id", {"inputTokens": 100, "outputTokens": 1,
                                    "totalTokens": 101})
    transcript = parse_bytes(_session_lines([record, record, record]))
    assert transcript.usage.calls == 1
    assert transcript.usage.input_tokens == 100
    assert transcript.usage.total_tokens == 101


def test_total_falls_back_to_input_plus_output():
    transcript = parse_bytes(_session_lines([
        _assistant("a", {"inputTokens": 4, "outputTokens": 6})]))
    assert transcript.usage.total_tokens == 10


def test_models_and_providers_are_collected_once():
    transcript = parse_bytes(_session_lines([
        _assistant("a", {"totalTokens": 1}, model="m"),
        _assistant("b", {"totalTokens": 1}, model="m"),
        _assistant("c", {"totalTokens": 1}, model="other"),
    ]))
    assert transcript.engine.models == ["m", "other"]
    assert transcript.engine.providers == ["local"]


def test_retries_are_counted():
    payload = _session_lines([{"type": "llm/retry", "seq": 2, "time": 1200},
                              {"type": "llm/retry-started", "seq": 3,
                               "time": 1201}])
    transcript = parse_bytes(payload)
    assert transcript.engine.retries == 2
    assert transcript.engine.event_counts["llm/retry"] == 1


# --- resilience -----------------------------------------------------------


def test_corrupt_line_is_reported_not_fatal():
    payload = _session_lines([_assistant("a", {"totalTokens": 5})])
    payload = payload.replace(b'"usage"', b'"usa###', 1)
    transcript = parse_bytes(payload)
    assert transcript.problems
    assert transcript.usage.calls == 0  # the damaged record is unusable


def test_unparseable_line_keeps_the_rest():
    payload = _session_lines([_assistant("a", {"totalTokens": 5})])
    payload = b"{not json}\n" + payload
    transcript = parse_bytes(payload)
    assert transcript.usage.calls == 1
    assert any("unparseable" in problem for problem in transcript.problems)


def test_non_object_line_is_reported():
    transcript = parse_bytes(b'"just a string"\n')
    assert any("not an object" in problem for problem in transcript.problems)


def test_truncated_log_still_yields_the_usable_part():
    """A partial log is evidence for the part that arrived."""
    payload = _session_lines([
        _assistant("a", {"totalTokens": 5}),
        _assistant("b", {"totalTokens": 7}),
    ])
    transcript = parse_bytes(payload[:len(payload) - 40])
    assert transcript.usage.calls == 1
    assert transcript.usage.total_tokens == 5
    assert any("unparseable" in problem for problem in transcript.problems)


def test_unsupported_version_is_flagged():
    payload = _session_lines([], header={"type": "session", "version": 99,
                                         "id": "s", "cwd": "/ws"})
    transcript = parse_bytes(payload)
    assert any("unsupported session version" in p
               for p in transcript.problems)


def test_empty_input_is_not_an_error():
    transcript = parse_bytes(b"")
    assert transcript.session_id == ""
    assert transcript.usage.calls == 0
    assert transcript.problems == []


def test_wall_seconds_needs_both_ends():
    payload = _session_lines([], header={"type": "session", "version": 3,
                                         "id": "s", "cwd": "/ws"})
    transcript = parse_bytes(payload)
    assert transcript.engine.wall_seconds() is None


# --- file handling --------------------------------------------------------


def test_missing_log_raises(tmp_path):
    with pytest.raises(TranscriptError):
        parse_session(tmp_path / "absent.jsonl.zstd")


def test_plain_jsonl_is_accepted(tmp_path):
    path = tmp_path / "session.v3.jsonl"
    path.write_bytes(_session_lines([_assistant("a", {"totalTokens": 3})]))
    assert parse_session(path).usage.total_tokens == 3


@requires_zstd
def test_undecompressable_log_raises(tmp_path):
    path = tmp_path / "broken.zstd"
    path.write_bytes(b"\x28\xb5\x2f\xfd definitely not zstd")
    with pytest.raises(TranscriptError):
        parse_session(path)


def test_read_lines_returns_records_for_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    assert read_lines(path) == []


# --- locating a run's own log --------------------------------------------


def _make_session(root: Path, cwd: str, session_id: str,
                  mtime: float | None = None) -> Path:
    encoded = "--" + cwd.strip("/").replace("/", "-") + "--"
    directory = root / encoded / f"session-{session_id}"
    directory.mkdir(parents=True)
    log = directory / "session.v3.jsonl.zstd"
    log.write_bytes(_session_lines([_assistant("a", {"totalTokens": 1})]))
    if mtime is not None:
        import os
        os.utime(log, (mtime, mtime))
    return log


def test_find_session_logs_filters_by_cwd(tmp_path):
    _make_session(tmp_path, "/home/user/proj-a", "aaa")
    _make_session(tmp_path, "/home/user/proj-b", "bbb")
    logs = find_session_logs(tmp_path, cwd="/home/user/proj-a")
    assert len(logs) == 1
    assert "aaa" in str(logs[0])


def test_newest_session_log_respects_started_after(tmp_path):
    old = _make_session(tmp_path, "/ws", "old", mtime=1000.0)
    new = _make_session(tmp_path, "/ws", "new", mtime=5000.0)
    assert newest_session_log(tmp_path, cwd="/ws") == new
    assert newest_session_log(tmp_path, cwd="/ws",
                              started_after=2000.0) == new
    assert newest_session_log(tmp_path, cwd="/ws",
                              started_after=6000.0) is None
    assert old.exists()


def test_newest_session_log_on_missing_root(tmp_path):
    assert newest_session_log(tmp_path / "absent") is None


def test_parse_records_accepts_an_iterable():
    lines = _session_lines([_assistant("a", {"totalTokens": 2})]).splitlines()
    assert parse_records(lines).usage.total_tokens == 2


def test_find_session_logs_accepts_a_plain_jsonl_log(tmp_path):
    """A runtime without stdlib zstd writes plain JSONL.

    Python < 3.14 has no compression.zstd, so a harness there writes
    session.v3.jsonl. A watcher that only globbed the compressed name found
    nothing and silently accounted zero tokens for the whole run.
    """
    directory = tmp_path / "--ws--" / "session-plain"
    directory.mkdir(parents=True)
    log = directory / "session.v3.jsonl"
    log.write_text(PLAIN.read_text())

    found = find_session_logs(tmp_path, cwd="/ws")
    assert found == [log]
    assert newest_session_log(tmp_path, cwd="/ws") == log
    assert parse_session(log).usage.total_tokens > 0


def test_compressed_form_is_preferred_when_both_exist(tmp_path):
    directory = tmp_path / "--ws--" / "session-both"
    directory.mkdir(parents=True)
    plain = directory / "session.v3.jsonl"
    plain.write_text(PLAIN.read_text())
    compressed = directory / "session.v3.jsonl.zstd"
    if HAS_ZSTD:
        import compression.zstd
        compressed.write_bytes(compression.zstd.compress(plain.read_bytes()))
    else:  # pragma: no cover - Python < 3.14
        compressed.write_bytes(b"not readable here")

    chosen = newest_session_log(tmp_path, cwd="/ws")
    assert chosen is not None
    if HAS_ZSTD:
        assert chosen.suffix == ".zstd"
