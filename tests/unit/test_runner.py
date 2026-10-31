"""End-to-end tests for the benchmark runner.

These drive the real stage → spawn → budget → grade → persist path with a stub
"agent" (``tests/fixtures/stub_agent.py``): no model, no network, no dsh. That
is the only way to test budget kills and tamper detection deterministically.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cupola.harness import TRANSPORT_HTTP, System
from cupola.runner import (
    STS_BREACHED,
    STS_ERROR,
    STS_FAILED,
    STS_TIMEOUT,
    STS_VERIFIED,
    RunnerConfig,
    RunnerError,
    fixture_hashes,
    preflight,
    run_suite,
    run_task,
    stage_workspace,
    tail_log,
)
from cupola.suites import load_suite

BENCH_ROOT = Path(__file__).resolve().parents[2] / "benchmarks"
STUB = Path(__file__).resolve().parents[1] / "fixtures" / "stub_agent.py"

SUITE = "coding-core-v2"
TASK = "t3_keyerror_default"


def _stub_system(name: str = "stub") -> System:
    """A process system that runs the stub script instead of dsh."""
    return System(name=name, command=[sys.executable, str(STUB), "{prompt}"])


def _config(tmp_path: Path, **overrides) -> RunnerConfig:
    values = {
        "state_dir": tmp_path / "state",
        "sessions_root": tmp_path / "stub-home" / "sessions",
        "token_poll_seconds": 0.05,
        "keep_workspace": True,
    }
    values.update(overrides)
    return RunnerConfig(**values)


def _env(monkeypatch, tmp_path: Path, **extra) -> None:
    monkeypatch.setenv("STUB_DSH_HOME", str(tmp_path / "stub-home"))
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


# --- staging --------------------------------------------------------------


def test_stage_workspace_preserves_relative_layout(tmp_path):
    suite = load_suite(SUITE, BENCH_ROOT)
    task = suite.task(TASK)
    workspace = stage_workspace(suite.root, task, into=tmp_path / "ws")
    assert (workspace / "tasks" / "keyerror_default" / "task.py").is_file()
    assert (workspace / "tasks" / "keyerror_default" / "test_task.py").is_file()
    # Nothing else leaks in: a fresh workspace contains only declared fixtures.
    assert sorted(p.name for p in (workspace / "tasks").iterdir()) == \
        ["keyerror_default"]


def test_stage_workspace_reports_a_missing_fixture(tmp_path):
    suite = load_suite(SUITE, BENCH_ROOT)
    task = suite.task(TASK)
    task.fixtures = ["tasks/keyerror_default/absent.py"]
    with pytest.raises(RunnerError):
        stage_workspace(suite.root, task, into=tmp_path / "ws")


def test_fixture_hashes_cover_every_entry(tmp_path):
    suite = load_suite(SUITE, BENCH_ROOT)
    task = suite.task(TASK)
    workspace = stage_workspace(suite.root, task, into=tmp_path / "ws")
    hashes = fixture_hashes(workspace, task)
    assert "tasks/keyerror_default/test_task.py" in hashes
    assert all(len(value) == 64 for value in hashes.values())


# --- the happy path -------------------------------------------------------


def test_run_task_verifies_a_solved_task(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    outcome = run_task(load_suite(SUITE, BENCH_ROOT).task(TASK),
                       load_suite(SUITE, BENCH_ROOT).root,
                       _stub_system(), _config(tmp_path))
    assert outcome.status == STS_VERIFIED, outcome.to_dict()
    assert outcome.passed
    assert all(grade.passed for grade in outcome.grades)
    assert outcome.exit_code == 0
    assert outcome.wall_seconds > 0


def test_run_task_records_token_usage_from_the_session_log(tmp_path,
                                                          monkeypatch):
    _env(monkeypatch, tmp_path)
    outcome = run_task(load_suite(SUITE, BENCH_ROOT).task(TASK),
                       load_suite(SUITE, BENCH_ROOT).root,
                       _stub_system(), _config(tmp_path))
    assert outcome.usage["calls"] == 1
    assert outcome.usage["total_tokens"] == 110
    assert outcome.usage["input_tokens"] == 100
    assert outcome.engine["models"] == ["stub-model"]


def test_run_task_fails_a_verifiably_broken_task(tmp_path, monkeypatch):
    """The stub does nothing, so the fixture still fails: an honest fail."""
    _env(monkeypatch, tmp_path, STUB_MODE="noop")
    outcome = run_task(load_suite(SUITE, BENCH_ROOT).task(TASK),
                       load_suite(SUITE, BENCH_ROOT).root,
                       _stub_system(), _config(tmp_path))
    assert outcome.status == STS_FAILED
    assert not outcome.passed
    assert any(not grade.passed for grade in outcome.grades)


def test_outcome_is_persisted_with_evidence(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    run_task(load_suite(SUITE, BENCH_ROOT).task(TASK),
             load_suite(SUITE, BENCH_ROOT).root,
             _stub_system(), _config(tmp_path))
    written = list((tmp_path / "state" / "runs").glob("*/outcome.json"))
    assert written, "the runner must leave an outcome on disk"
    payload = json.loads(written[0].read_text())
    assert payload["task_id"] == TASK
    assert payload["grades"]


# --- budgets --------------------------------------------------------------


def test_wall_clock_budget_kills_a_hanging_agent(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path, STUB_MODE="hang")
    suite = load_suite(SUITE, BENCH_ROOT)
    task = suite.task(TASK)
    task.budgets = type(task.budgets)(wall_seconds=2, token_budget=10**9,
                                      max_turns=10**6)
    outcome = run_task(task, suite.root, _stub_system(), _config(tmp_path))
    assert outcome.status == STS_TIMEOUT
    assert outcome.wall_seconds < 30, "the kill must actually stop the agent"
    assert any("wall-clock budget" in problem for problem in outcome.problems)


def test_token_budget_breach_is_distinct_from_a_timeout(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path, STUB_MODE="overspend")
    suite = load_suite(SUITE, BENCH_ROOT)
    task = suite.task(TASK)
    task.budgets = type(task.budgets)(wall_seconds=120, token_budget=500,
                                      max_turns=10**6)
    outcome = run_task(task, suite.root, _stub_system(), _config(tmp_path))
    assert outcome.status == STS_BREACHED
    assert any("token budget exceeded" in problem
               for problem in outcome.problems)
    assert outcome.usage["total_tokens"] > 500


def test_turn_budget_breach(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path, STUB_MODE="overspend")
    suite = load_suite(SUITE, BENCH_ROOT)
    task = suite.task(TASK)
    task.budgets = type(task.budgets)(wall_seconds=120,
                                      token_budget=10**12, max_turns=2)
    outcome = run_task(task, suite.root, _stub_system(), _config(tmp_path))
    assert outcome.status == STS_BREACHED
    assert any("turn budget exceeded" in problem
               for problem in outcome.problems)


# --- tamper detection -----------------------------------------------------


def test_rewriting_the_acceptance_file_does_not_pass(tmp_path, monkeypatch):
    """An agent that edits its own test has not solved the task."""
    _env(monkeypatch, tmp_path)
    suite = load_suite(SUITE, BENCH_ROOT)
    task = suite.task(TASK)
    # A stub that "fixes" the test instead of the code.
    cheater = tmp_path / "cheat.py"
    cheater.write_text(
        "import pathlib\n"
        "p = pathlib.Path('tasks/keyerror_default/test_task.py')\n"
        "p.write_text('def test_missing_key_returns_none():\\n"
        "    assert True\\n')\n")
    system = System(name="cheater",
                    command=[sys.executable, str(cheater), "{prompt}"])
    outcome = run_task(task, suite.root, system, _config(tmp_path))
    assert outcome.status == STS_FAILED
    assert any("changed after the run started" in grade.evidence
               or "unmodified" in grade.criterion
               for grade in outcome.grades)


# --- failure modes --------------------------------------------------------


def test_missing_command_is_reported_not_crashed(tmp_path):
    system = System(name="ghost", command=["definitely-not-a-real-binary-xyz",
                                           "{prompt}"])
    outcome = run_task(load_suite(SUITE, BENCH_ROOT).task(TASK),
                       load_suite(SUITE, BENCH_ROOT).root,
                       system, _config(tmp_path))
    assert outcome.status == STS_ERROR
    assert any("not found on PATH" in problem for problem in outcome.problems)


def test_invalid_system_is_rejected_before_staging(tmp_path):
    outcome = run_task(load_suite(SUITE, BENCH_ROOT).task(TASK),
                       load_suite(SUITE, BENCH_ROOT).root,
                       System(name="no-command"), _config(tmp_path))
    assert outcome.status == STS_ERROR
    assert outcome.problems


def test_run_suite_rejects_an_unknown_task(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    with pytest.raises(RunnerError):
        run_suite(SUITE, _stub_system(), BENCH_ROOT, task_id="ghost",
                  config=_config(tmp_path))


# --- suite-level runs -----------------------------------------------------


def test_run_suite_verifies_every_task(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    outcomes = run_suite(SUITE, _stub_system(), BENCH_ROOT,
                         config=_config(tmp_path))
    assert [o.task_id for o in outcomes] == ["t3_keyerror_default",
                                             "t3_assertion_expectation"]
    assert all(o.status == STS_VERIFIED for o in outcomes), \
        [o.to_dict() for o in outcomes]


def test_run_suite_repeats_for_independent_runs(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    outcomes = run_suite(SUITE, _stub_system(), BENCH_ROOT, task_id=TASK,
                         runs=3, config=_config(tmp_path))
    assert len(outcomes) == 3
    assert len({o.run_id for o in outcomes}) == 3, \
        "each run must get its own identity"


# --- helpers --------------------------------------------------------------


def test_tail_log_reads_the_end_of_a_log(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text("x" * 10_000 + "TAIL")
    assert tail_log(log, limit=100).endswith("TAIL")


def test_tail_log_on_missing_file_is_empty(tmp_path):
    assert tail_log(tmp_path / "absent.log") == ""


def test_preflight_flags_an_unreachable_endpoint():
    system = System(name="http", transport=TRANSPORT_HTTP,
                    base_url="http://127.0.0.1:9/v1")
    problems = preflight(system)
    assert problems and "not reachable" in problems[0]


def test_preflight_flags_a_missing_binary():
    problems = preflight(System(name="g", command=["not-a-real-binary-xyz",
                                                   "{prompt}"]))
    assert problems and "not found on PATH" in problems[0]
