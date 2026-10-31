"""Tests for benchmark suite manifests and the bench CLI surface.

The loader must reject a manifest that cannot actually be run (missing
fixture, unknown grader, no kickoff) rather than failing later mid-run, and
the fingerprint must move only when the functional core moves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cupola import suites as suites_mod
from cupola.cli import main
from cupola.suites import (
    FORBIDDEN_VOCABULARY,
    SuiteError,
    benchmarks_root,
    list_suites,
    load_suite,
    plan_run,
)


def _write_suite(root: Path, suite_id: str, tasks: list[dict],
                 domain: str = "coding") -> Path:
    suite_dir = root / suite_id
    (suite_dir / "tasks").mkdir(parents=True, exist_ok=True)
    for task in tasks:
        kickoff = task.get("kickoff")
        if kickoff:
            target = suite_dir / kickoff
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# Task\n")
        for rel in task.get("fixtures") or []:
            target = suite_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x = 1\n")
    (suite_dir / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "suite_id": suite_id,
        "domain": domain,
        "title": "Test suite",
        "tasks": tasks,
    }))
    return suite_dir


def _task(**overrides) -> dict:
    task = {
        "task_id": "t1",
        "kind": "coding",
        "title": "One task",
        "kickoff": "tasks/t1/kickoff.md",
        "fixtures": ["tasks/t1/fixture.py"],
        "graders": [{"type": "pytest", "entry": ["test_task.py"]}],
        "expected": "tests_pass",
        "forbidden": list(FORBIDDEN_VOCABULARY),
        "budgets": {"wall_seconds": 60, "token_budget": 1000, "max_turns": 2},
    }
    task.update(overrides)
    return task


# --- loading and validation ----------------------------------------------


def test_committed_suite_is_valid():
    suite = load_suite("coding-core-v2", Path("benchmarks"))
    assert suite.errors == []
    assert suite.domain == "coding"
    assert len(suite.tasks) == 2


def test_valid_manifest_from_synthetic_root(tmp_path):
    _write_suite(tmp_path, "demo-v1", [_task()])
    suite = load_suite("demo-v1", tmp_path)
    assert suite.errors == []
    assert suite.tasks[0].graders[0].type == "pytest"


def test_missing_kickoff_is_reported(tmp_path):
    _write_suite(tmp_path, "demo-v1", [_task()])
    (tmp_path / "demo-v1" / "tasks" / "t1" / "kickoff.md").unlink()
    suite = load_suite("demo-v1", tmp_path)
    assert any("missing kickoff" in error for error in suite.errors)


def test_missing_fixture_is_reported(tmp_path):
    _write_suite(tmp_path, "demo-v1", [_task()])
    (tmp_path / "demo-v1" / "tasks" / "t1" / "fixture.py").unlink()
    suite = load_suite("demo-v1", tmp_path)
    assert any("missing path" in error for error in suite.errors)


def test_unknown_grader_type_is_rejected(tmp_path):
    _write_suite(tmp_path, "demo-v1",
                 [_task(graders=[{"type": "vibes"}])])
    suite = load_suite("demo-v1", tmp_path)
    assert any("unknown grader type" in error for error in suite.errors)


def test_forbidden_vocabulary_is_closed(tmp_path):
    _write_suite(tmp_path, "demo-v1",
                 [_task(forbidden=["network", "teleport"])])
    suite = load_suite("demo-v1", tmp_path)
    assert any("closed vocabulary" in error for error in suite.errors)


def test_duplicate_task_ids_rejected(tmp_path):
    _write_suite(tmp_path, "demo-v1", [_task(), _task()])
    suite = load_suite("demo-v1", tmp_path)
    assert any("duplicate task_id" in error for error in suite.errors)


def test_unknown_domain_rejected(tmp_path):
    _write_suite(tmp_path, "demo-v1", [_task()], domain="astrology")
    suite = load_suite("demo-v1", tmp_path)
    assert any("domain must be one of" in error for error in suite.errors)


def test_task_kind_must_match_suite_domain(tmp_path):
    _write_suite(tmp_path, "demo-v1", [_task(kind="resume")], domain="coding")
    suite = load_suite("demo-v1", tmp_path)
    assert any("outside the suite domain" in error for error in suite.errors)


def test_zero_budget_rejected(tmp_path):
    _write_suite(tmp_path, "demo-v1",
                 [_task(budgets={"wall_seconds": 0, "token_budget": 10,
                                 "max_turns": 1})])
    suite = load_suite("demo-v1", tmp_path)
    assert any("wall_seconds must be > 0" in error for error in suite.errors)


def test_missing_suite_raises(tmp_path):
    with pytest.raises(SuiteError):
        load_suite("ghost", tmp_path)


def test_malformed_manifest_raises(tmp_path):
    suite_dir = tmp_path / "broken"
    suite_dir.mkdir()
    (suite_dir / "manifest.json").write_text("{ not json")
    with pytest.raises(SuiteError):
        load_suite("broken", tmp_path)


# --- fingerprint semantics ------------------------------------------------


def test_fingerprint_ignores_cosmetic_edits(tmp_path):
    _write_suite(tmp_path, "demo-v1", [_task(title="Original title")])
    before = load_suite("demo-v1", tmp_path).fingerprint()
    _write_suite(tmp_path, "demo-v1", [_task(title="Re-worded title")])
    after = load_suite("demo-v1", tmp_path).fingerprint()
    assert before == after, "a title edit must not break comparability"


def test_fingerprint_moves_when_a_task_changes(tmp_path):
    _write_suite(tmp_path, "demo-v1", [_task()])
    before = load_suite("demo-v1", tmp_path).fingerprint()
    _write_suite(tmp_path, "demo-v1", [_task(kickoff="tasks/t2/kickoff.md")])
    after = load_suite("demo-v1", tmp_path).fingerprint()
    assert before != after


def test_fingerprint_is_deterministic(tmp_path):
    _write_suite(tmp_path, "demo-v1", [_task()])
    first = load_suite("demo-v1", tmp_path).fingerprint()
    second = load_suite("demo-v1", tmp_path).fingerprint()
    assert first == second


# --- listing and planning -------------------------------------------------


def test_list_suites_never_raises_on_a_broken_suite(tmp_path):
    _write_suite(tmp_path, "good-v1", [_task()])
    broken = tmp_path / "bad-v1"
    broken.mkdir()
    (broken / "manifest.json").write_text("{}")
    rows = {row["suite_id"]: row for row in list_suites(tmp_path)}
    assert rows["good-v1"]["status"] == "ok"
    assert rows["bad-v1"]["status"].startswith("invalid")


def test_list_suites_on_missing_root_is_empty(tmp_path):
    assert list_suites(tmp_path / "absent") == []


def test_plan_run_covers_all_tasks(tmp_path):
    _write_suite(tmp_path, "demo-v1", [_task(task_id="a"), _task(task_id="b")])
    plan = plan_run(load_suite("demo-v1", tmp_path), system="local")
    assert [entry["task_id"] for entry in plan["tasks"]] == ["a", "b"]
    assert plan["dry_run"] is True
    assert plan["estimated_sessions"] == 2
    assert plan["writes"].startswith("nothing")


def test_plan_run_single_task_and_run_multiplier(tmp_path):
    _write_suite(tmp_path, "demo-v1", [_task(task_id="a"), _task(task_id="b")])
    plan = plan_run(load_suite("demo-v1", tmp_path), task_id="b",
                    system="local", runs=3)
    assert [entry["task_id"] for entry in plan["tasks"]] == ["b"]
    assert plan["estimated_sessions"] == 3


def test_plan_run_flags_unknown_task_and_missing_system(tmp_path):
    _write_suite(tmp_path, "demo-v1", [_task()])
    plan = plan_run(load_suite("demo-v1", tmp_path), task_id="ghost")
    assert plan["tasks"] == []
    notes = " ".join(plan["notes"])
    assert "unknown task" in notes
    assert "no --system" in notes


def test_benchmarks_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CUPOLA_BENCHMARKS_DIR", str(tmp_path))
    assert benchmarks_root() == tmp_path
    monkeypatch.delenv("CUPOLA_BENCHMARKS_DIR")
    assert benchmarks_root().name == "benchmarks"


# --- CLI surface ----------------------------------------------------------


def test_cli_suite_list(capsys):
    assert main(["bench", "suite", "list"]) == 0
    assert "coding-core-v2" in capsys.readouterr().out


def test_cli_suite_list_json(capsys):
    assert main(["bench", "suite", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["suites"][0]["suite_id"] == "coding-core-v2"


def test_cli_suite_validate_ok(capsys):
    assert main(["bench", "suite", "validate", "coding-core-v2"]) == 0
    assert "is valid" in capsys.readouterr().out


def test_cli_suite_validate_unknown_is_an_error(capsys):
    assert main(["bench", "suite", "validate", "ghost"]) == 1
    assert "unknown suite" in capsys.readouterr().err


def test_cli_suite_run_is_dry_run_by_default(capsys):
    assert main(["bench", "suite", "run", "--suite", "coding-core-v2",
                 "--system", "local-headless"]) == 0
    out = capsys.readouterr().out
    assert "t3_keyerror_default" in out
    assert "nothing" in out or "Runs per task" in out


def test_cli_lane_list_json(capsys, tmp_path):
    home = _fake_home(tmp_path)
    rc = main(["bench", "lane", "list", "--profile", "demo",
               "--dsh-home", str(home), "--workspace", str(tmp_path),
               "--no-dump", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "demo"
    assert any(lane["kind"] == "bundle" for lane in payload["lanes"])
    assert payload["lane_set_hash"]


def test_cli_lane_diff_reports_a_change(capsys, tmp_path):
    home = _fake_home(tmp_path)
    out = tmp_path / "snaps"
    common = ["bench", "lane", "snapshot", "--profile", "demo",
              "--dsh-home", str(home), "--workspace", str(tmp_path),
              "--no-dump", "--out", str(out), "--json"]
    assert main(common) == 0
    first = json.loads(capsys.readouterr().out)["path"]
    skill = home / "skills" / "api-review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: api-review\n---\n")
    assert main(common) == 0
    second = json.loads(capsys.readouterr().out)["path"]
    assert first != second, "snapshots must not overwrite within one second"
    assert main(["bench", "lane", "diff", first, second]) == 0
    assert "skill:api-review" in capsys.readouterr().out


def test_cli_legacy_bench_profile_form_still_routes(capsys):
    """`cupola bench <profile>` must keep working after the subcommand split."""
    rc = main(["bench", "nosuchprofile"])
    assert rc == 1
    assert "not found in configuration" in capsys.readouterr().out


def _fake_home(root: Path) -> Path:
    home = root / "dsh"
    profile = home / "profiles" / "demo"
    profile.mkdir(parents=True)
    (profile / "package.json").write_text(json.dumps({
        "name": "dsh-profile-demo",
        "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}},
        "dependencies": {},
    }))
    (home / "AGENTS.md").write_text("# Rules\n")
    return home


def test_suites_module_exposes_domain_vocabulary():
    assert "compliance_trade" in suites_mod.DOMAINS
    assert "compliance_banking" in suites_mod.DOMAINS
    assert "trading_research" in suites_mod.DOMAINS
    assert "perf" in suites_mod.TASK_KINDS
