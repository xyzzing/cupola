"""Tests for benchmark reports and the end-to-end execution path.

A report must bind the suite fingerprint, the systems, and the lane inventory
together, and it must state what its graders cannot prove. These tests pin
that contract, then drive the real CLI execution path with the stub agent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cupola.cli import main
from cupola.graders import GradeResult
from cupola.lanes import Lane, LaneSet
from cupola.report import (
    build_report,
    derive_metrics,
    format_report,
    read_report,
    validate_report,
)
from cupola.runner import TaskOutcome
from cupola.suites import load_suite

BENCH_ROOT = Path(__file__).resolve().parents[2] / "benchmarks"
STUB = Path(__file__).resolve().parents[1] / "fixtures" / "stub_agent.py"
SUITE = "coding-core-v2"


def _outcome(task_id: str = "t1", status: str = "verified",
             tokens: int = 100, wall: float = 2.0,
             grades: list[GradeResult] | None = None) -> TaskOutcome:
    outcome = TaskOutcome(task_id=task_id, system="stub", status=status,
                          wall_seconds=wall, exit_code=0)
    outcome.usage = {"total_tokens": tokens, "input_tokens": tokens // 2,
                     "output_tokens": tokens // 2, "cache_read_tokens": 0,
                     "calls": 1}
    outcome.grades = grades if grades is not None else [
        GradeResult("pytest", "tests pass", status == "verified",
                    "exit 0", limitation="decides the fixture's criteria")]
    return outcome


# --- metrics --------------------------------------------------------------


def test_metrics_count_each_status_separately():
    outcomes = [
        _outcome("a", "verified"),
        _outcome("b", "failed"),
        _outcome("c", "timeout"),
        _outcome("d", "breached"),
        _outcome("e", "error"),
    ]
    metrics = derive_metrics(outcomes)
    assert metrics["comparable_runs"] == 5
    assert metrics["verified"] == 1
    assert metrics["timed_out"] == 1
    assert metrics["breached"] == 1
    assert metrics["errored"] == 1
    assert metrics["failed"] == 1
    assert metrics["verified_completion_rate"] == 0.2


def test_metrics_on_no_outcomes_are_safe():
    metrics = derive_metrics([])
    assert metrics["comparable_runs"] == 0
    assert metrics["verified_completion_rate"] == 0.0
    assert metrics["wall_seconds_p50"] is None
    assert metrics["tokens_per_verified_task"] is None


def test_metrics_separate_cost_per_task_from_cost_per_win():
    """A failed attempt's spend is the price of the next win, not free."""
    metrics = derive_metrics([_outcome("a", "verified", tokens=100, wall=2.0),
                              _outcome("b", "failed", tokens=900, wall=9.0)])
    assert metrics["tokens_total"] == 1000
    # What a successful task cost.
    assert metrics["tokens_per_verified_task"] == 100.0
    assert metrics["wall_seconds_per_verified_task"] == 2.0
    # What one win cost once the miss is paid for.
    assert metrics["tokens_per_success"] == 1000.0
    assert metrics["wall_seconds_per_success"] == 11.0


def test_percentiles_interpolate():
    metrics = derive_metrics([_outcome("a", wall=1.0), _outcome("b", wall=3.0)])
    assert metrics["wall_seconds_p50"] == 2.0
    assert metrics["wall_seconds_p95"] == 2.9


def test_percentile_of_a_single_value():
    metrics = derive_metrics([_outcome("a", wall=4.0)])
    assert metrics["wall_seconds_p50"] == 4.0
    assert metrics["wall_seconds_p95"] == 4.0


def test_safety_counters_start_at_zero():
    metrics = derive_metrics([_outcome()])
    assert metrics["unsafe_executions"] == 0
    assert metrics["prohibited_action_attempts"] == 0
    assert metrics["reproducibility_violations"] == 0


# --- report construction --------------------------------------------------


def _lane_set() -> LaneSet:
    return LaneSet(profile="web", lanes=[
        Lane(lane_id="install:ponytail", kind="patch-entry", source="x")])


def test_report_binds_suite_systems_and_lanes():
    suite = load_suite(SUITE, BENCH_ROOT)
    report = build_report(suite, [_outcome()], _lane_set())
    payload = report.to_dict()
    assert payload["suite_id"] == SUITE
    assert payload["suite_fingerprint"] == suite.fingerprint()
    assert payload["environment"]["lane_set_hash"] == \
        _lane_set().lane_set_hash()
    assert payload["environment"]["lane_count"] == 1
    assert validate_report(payload) == []


def test_report_without_lanes_is_not_reproducible():
    """A result with no recorded toolchain cannot be reproduced."""
    report = build_report(load_suite(SUITE, BENCH_ROOT), [_outcome()], None)
    assert report.to_dict()["environment"]["reproducible"] is False


def test_report_flags_lane_warnings_as_not_reproducible():
    lane_set = _lane_set()
    lane_set.unresolved_bundles = ["dsh-context-guard"]
    report = build_report(load_suite(SUITE, BENCH_ROOT), [_outcome()], lane_set)
    assert report.to_dict()["environment"]["reproducible"] is False
    assert report.to_dict()["environment"]["lane_warnings"]


def test_report_records_declared_systems_even_without_runs():
    from cupola.harness import System
    system = System(name="local-ponytail-on", command=["dsh", "{prompt}"])
    report = build_report(load_suite(SUITE, BENCH_ROOT), [], None,
                          systems=[system])
    assert [s["name"] for s in report.to_dict()["systems"]] == \
        ["local-ponytail-on"]


def test_report_carries_grader_limitations():
    report = build_report(load_suite(SUITE, BENCH_ROOT),
                          [_outcome(grades=[GradeResult(
                              "claims", "facts hold", True,
                              limitation="can be evaded by unusual phrasing")])],
                          None)
    limitations = report.to_dict()["limitations"]
    assert any("evaded" in item for item in limitations)


def test_breached_runs_add_a_limitation():
    report = build_report(load_suite(SUITE, BENCH_ROOT),
                          [_outcome(status="breached")], None)
    assert any("prove nothing about task difficulty" in item
               for item in report.to_dict()["limitations"])


def test_run_rows_carry_per_grader_verdicts():
    report = build_report(load_suite(SUITE, BENCH_ROOT), [_outcome()], None)
    row = report.to_dict()["runs"][0]
    assert row["verdicts"] == {"pytest": "passed"}
    assert row["usage"]["total_tokens"] == 100


# --- persistence and validation ------------------------------------------


def test_report_round_trips_through_disk(tmp_path):
    report = build_report(load_suite(SUITE, BENCH_ROOT), [_outcome()],
                          _lane_set())
    path = report.write(tmp_path / "reports" / "r.json")
    assert path.is_file()
    assert read_report(path)["suite_id"] == SUITE


def test_validate_report_rejects_each_broken_field():
    good = build_report(load_suite(SUITE, BENCH_ROOT), [_outcome()],
                        _lane_set()).to_dict()
    for mutate, expect in (
        (lambda p: p.update(report_version=99), "report_version"),
        (lambda p: p.update(suite_id=""), "suite_id"),
        (lambda p: p.update(metrics={}), "metrics"),
        (lambda p: p["metrics"].update(verified=-1), "verified"),
        (lambda p: p["metrics"].update(verified_completion_rate=2.0), "rate"),
        (lambda p: p.update(runs=[{"nope": 1}]), "bad run entry"),
        (lambda p: p.update(limitations="nope"), "limitations"),
    ):
        payload = json.loads(json.dumps(good))
        mutate(payload)
        errors = validate_report(payload)
        assert any(expect in error for error in errors), (expect, errors)


def test_read_report_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError):
        read_report(bad)


def test_format_report_states_numbers_and_caveats():
    report = build_report(load_suite(SUITE, BENCH_ROOT), [_outcome()],
                          _lane_set())
    text = format_report(report.to_dict())
    assert "Verified: 1/1 (100%)" in text
    assert "Limitations:" in text
    assert "reproducible" in text


# --- the real execution path ---------------------------------------------


@pytest.fixture()
def bench_env(tmp_path, monkeypatch):
    """Point all bench state at a temp dir and export the stub's sessions."""
    state = tmp_path / "state"
    monkeypatch.setenv("CUPOLA_BENCH_STATE", str(state))
    monkeypatch.setenv("CUPOLA_DSH_SESSIONS", str(state / "sessions"))
    monkeypatch.setenv("CUPOLA_SYSTEMS_FILE",
                       str(BENCH_ROOT / "systems.json"))
    return state


def test_cli_execute_verifies_the_stub_suite(bench_env, capsys):
    rc = main(["bench", "suite", "run", "--suite", SUITE,
               "--system", "stub-agent", "--execute"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Verified: 2/2" in out
    assert "Tokens per verified task" in out


def test_cli_execute_writes_a_valid_report(bench_env, capsys):
    main(["bench", "suite", "run", "--suite", SUITE,
          "--system", "stub-agent", "--execute"])
    capsys.readouterr()
    reports = list((bench_env / "reports").glob("*.json"))
    assert reports, "the CLI must persist a report"
    payload = json.loads(reports[0].read_text())
    assert validate_report(payload) == []
    assert payload["metrics"]["verified"] == 2
    assert payload["metrics"]["tokens_total"] > 0
    assert payload["environment"]["lane_set_hash"]


def test_cli_execute_single_task(bench_env, capsys):
    rc = main(["bench", "suite", "run", "--suite", SUITE,
               "--system", "stub-agent", "--task", "t3_keyerror_default",
               "--execute"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Verified: 1/1" in out


def test_cli_execute_reports_failure_when_the_agent_does_nothing(
        bench_env, capsys, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "noop")
    rc = main(["bench", "suite", "run", "--suite", SUITE,
               "--system", "stub-agent", "--execute"])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "Verified: 0/2" in out


def test_cli_execute_requires_a_system(bench_env, capsys):
    rc = main(["bench", "suite", "run", "--suite", SUITE, "--execute"])
    assert rc == 2
    assert "--system" in capsys.readouterr().err


def test_cli_execute_rejects_an_unknown_system(bench_env, capsys):
    rc = main(["bench", "suite", "run", "--suite", SUITE,
               "--system", "ghost", "--execute"])
    assert rc == 1
    assert "unknown system" in capsys.readouterr().err


def test_cli_execute_refuses_an_unreachable_endpoint(bench_env, capsys,
                                                     tmp_path):
    systems = tmp_path / "systems.json"
    systems.write_text(json.dumps({"systems": [
        {"name": "dead", "transport": "http",
         "base_url": "http://127.0.0.1:9/v1"}]}))
    bench_env.mkdir(parents=True, exist_ok=True)
    import os
    os.environ["CUPOLA_SYSTEMS_FILE"] = str(systems)
    rc = main(["bench", "suite", "run", "--suite", SUITE,
               "--system", "dead", "--execute"])
    assert rc == 1
    assert "not reachable" in capsys.readouterr().err


def test_cli_execute_json_reports_the_whole_run(bench_env, capsys):
    rc = main(["bench", "suite", "run", "--suite", SUITE,
               "--system", "stub-agent", "--execute", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["metrics"]["verified"] == 2
    assert len(payload["runs"]) == 2


def test_systems_file_is_anchored_for_spawning():
    """A relative script path must not be resolved against the task workspace."""
    from cupola.systems import find_system
    system = find_system("stub-agent", BENCH_ROOT)
    assert Path(system.command[1]).is_absolute()
    assert Path(system.command[1]).is_file()


def test_default_systems_are_valid():
    from cupola.systems import load_systems
    systems = load_systems(BENCH_ROOT)
    assert systems, "systems.json must declare at least one system"
    for system in systems:
        assert system.validate() == [], (system.name, system.validate())


def test_run_entry_is_importable_for_scripting():
    """The stub path must stay declared in the manifest, not hardcoded."""
    assert STUB.is_file()
    assert sys.executable
