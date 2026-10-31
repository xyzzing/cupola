"""Tests for acceptance graders and the system-under-test abstraction.

Each grader needs a passing and a failing case, because a grader that only
ever passes would make every benchmark result meaningless. Malformed specs
must fail loudly rather than defaulting to success.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cupola.graders import (
    GRADERS,
    GradeContext,
    grade,
    grade_checklist,
    grade_claims,
    grade_command,
    grade_perf,
    grade_pytest,
    grade_recompute,
    grade_schema,
    overall,
)
from cupola.harness import (
    PONYTAIL_OFF,
    PONYTAIL_ON,
    TRANSPORT_HTTP,
    TRANSPORT_PROCESS,
    System,
    build_argv,
    ponytail_arm,
    resolved_binary,
    system_from_dict,
)
from cupola.suites import GraderSpec


def _ctx(tmp_path: Path) -> GradeContext:
    return GradeContext(workspace=tmp_path, suite_root=tmp_path, timeout=30)


# --- command --------------------------------------------------------------


def test_command_grader_passes_on_zero_exit(tmp_path):
    result = grade_command(
        GraderSpec("command", {"argv": [sys.executable, "-c", "pass"]}),
        _ctx(tmp_path))
    assert result.passed
    assert "exit 0" in result.evidence


def test_command_grader_fails_on_nonzero_exit(tmp_path):
    result = grade_command(
        GraderSpec("command", {"argv": [sys.executable, "-c", "raise SystemExit(3)"]}),
        _ctx(tmp_path))
    assert not result.passed
    assert "exit 3" in result.evidence


def test_command_grader_rejects_malformed_spec(tmp_path):
    with pytest.raises(Exception):
        grade_command(GraderSpec("command", {"argv": "not-a-list"}),
                      _ctx(tmp_path))


def test_command_grader_never_uses_a_shell(tmp_path):
    """A shell metacharacter must arrive as a literal argument."""
    marker = tmp_path / "should-not-exist"
    result = grade_command(
        GraderSpec("command", {"argv": [
            sys.executable, "-c", "import sys; print(sys.argv[1])",
            f"; touch {marker}"]}),
        _ctx(tmp_path))
    assert result.passed
    assert not marker.exists(), "argv must not be shell-interpreted"


# --- pytest ---------------------------------------------------------------


def test_pytest_grader_passes_and_fails(tmp_path):
    (tmp_path / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_mod.py").write_text(
        "from mod import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    spec = GraderSpec("pytest", {"entry": ["test_mod.py"]})
    assert grade_pytest(spec, _ctx(tmp_path)).passed

    (tmp_path / "mod.py").write_text("def add(a, b):\n    return a - b\n")
    failed = grade_pytest(spec, _ctx(tmp_path))
    assert not failed.passed
    assert "exit 1" in failed.evidence


def test_pytest_grader_handles_a_nested_entry(tmp_path):
    work = tmp_path / "tasks" / "one"
    work.mkdir(parents=True)
    (work / "test_task.py").write_text("def test_ok():\n    assert True\n")
    result = grade_pytest(
        GraderSpec("pytest", {"entry": ["tasks/one/test_task.py"]}),
        _ctx(tmp_path))
    assert result.passed, result.evidence


def test_pytest_grader_reports_a_missing_entry(tmp_path):
    result = grade_pytest(GraderSpec("pytest", {"entry": ["nope/test_x.py"]}),
                          _ctx(tmp_path))
    assert not result.passed
    assert "not found in the workspace" in result.evidence


def test_pytest_grader_rejects_malformed_spec(tmp_path):
    with pytest.raises(Exception):
        grade_pytest(GraderSpec("pytest", {"entry": []}), _ctx(tmp_path))


def test_pytest_grader_does_not_collect_sibling_tasks(tmp_path):
    """One task's grade must not depend on another task's files.

    Both tasks declare the same entry name; only the selected task's file may
    be collected, or the failing sibling would sink the passing task.
    """
    for name, body in (("passing", "def test_ok():\n    assert True\n"),
                       ("failing", "def test_bad():\n    assert False\n")):
        work = tmp_path / name
        work.mkdir()
        (work / "test_task.py").write_text(body)

    spec = GraderSpec("pytest", {"entry": ["test_task.py"]})
    ctx = GradeContext(workspace=tmp_path / "passing", timeout=30)
    result = grade_pytest(spec, ctx)
    assert result.passed, result.evidence


def test_pytest_grader_detects_a_rewritten_acceptance_file(tmp_path):
    """Greening the suite by editing it must not count as a pass."""
    test_file = tmp_path / "test_task.py"
    test_file.write_text("def test_x():\n    assert True\n")
    from cupola.lanes import sha256_file
    honest = sha256_file(test_file)

    spec = GraderSpec("pytest", {
        "entry": ["test_task.py"],
        "fixture_hashes": {"test_task.py": honest}})
    assert grade_pytest(spec, _ctx(tmp_path)).passed

    test_file.write_text("def test_x():\n    assert True\n# edited\n")
    result = grade_pytest(spec, _ctx(tmp_path))
    assert not result.passed
    assert "changed after the run started" in result.evidence


def test_pytest_grader_detects_a_deleted_acceptance_file(tmp_path):
    """A deleted acceptance file cannot be treated as a pass."""
    result = grade_pytest(GraderSpec("pytest", {
        "entry": ["test_task.py"],
        "fixture_hashes": {"test_task.py": "0" * 64}}), _ctx(tmp_path))
    assert not result.passed
    assert "not found in the workspace" in result.evidence


# --- schema ---------------------------------------------------------------


def test_schema_grader_accepts_required_shape(tmp_path):
    (tmp_path / "out.json").write_text(json.dumps(
        {"question": "q", "rows": [{"a": 1}]}))
    result = grade_schema(GraderSpec("schema", {
        "path": "out.json", "required_keys": ["question"],
        "required_list_keys": ["rows"]}), _ctx(tmp_path))
    assert result.passed


def test_schema_grader_reports_each_problem(tmp_path):
    (tmp_path / "out.json").write_text(json.dumps({"rows": []}))
    result = grade_schema(GraderSpec("schema", {
        "path": "out.json", "required_keys": ["question"],
        "required_list_keys": ["rows"]}), _ctx(tmp_path))
    assert not result.passed
    assert "question" in result.evidence
    assert "rows" in result.evidence


def test_schema_grader_min_items(tmp_path):
    (tmp_path / "out.json").write_text(json.dumps({"risks": [{"id": 1}]}))
    result = grade_schema(GraderSpec("schema", {
        "path": "out.json", "min_items": {"risks": 3}}), _ctx(tmp_path))
    assert not result.passed
    assert "at least 3" in result.evidence


def test_schema_grader_missing_artifact_fails(tmp_path):
    result = grade_schema(GraderSpec("schema", {"path": "nope.json"}),
                          _ctx(tmp_path))
    assert not result.passed
    assert "was not produced" in result.evidence


def test_schema_grader_rejects_invalid_json(tmp_path):
    (tmp_path / "out.json").write_text("{not json")
    result = grade_schema(GraderSpec("schema", {"path": "out.json"}),
                          _ctx(tmp_path))
    assert not result.passed


# --- claims ---------------------------------------------------------------


def test_claims_grader_requires_and_forbids_text(tmp_path):
    (tmp_path / "resume.md").write_text("Led the rollout. Cert: CFA.")
    ok = grade_claims(GraderSpec("claims", {
        "path": "resume.md", "must_contain": ["Led the rollout"],
        "must_not_contain": ["sole author"]}), _ctx(tmp_path))
    assert ok.passed
    bad = grade_claims(GraderSpec("claims", {
        "path": "resume.md", "must_not_contain": ["Led the rollout"]}),
        _ctx(tmp_path))
    assert not bad.passed


def test_claims_grader_regex_assertions(tmp_path):
    (tmp_path / "a.txt").write_text("value 42")
    assert grade_claims(GraderSpec("claims", {
        "path": "a.txt", "must_match": [r"value \d+"]}), _ctx(tmp_path)).passed
    assert not grade_claims(GraderSpec("claims", {
        "path": "a.txt", "must_not_match": [r"v\w+"]}), _ctx(tmp_path)).passed


def test_claims_grader_citation_must_resolve_in_corpus(tmp_path):
    """Traceability, not recall: citations must exist in the supplied corpus."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "obligation.txt").write_text("Customer due diligence required.")
    (tmp_path / "brief.md").write_text(
        "Customer due diligence required [obligation.txt].")
    ok = grade_claims(GraderSpec("claims", {
        "path": "brief.md", "corpus": "corpus"}), _ctx(tmp_path))
    assert ok.passed

    (tmp_path / "brief.md").write_text("Required [invented-clause-99].")
    bad = grade_claims(GraderSpec("claims", {
        "path": "brief.md", "corpus": "corpus"}), _ctx(tmp_path))
    assert not bad.passed
    assert "not found in supplied corpus" in bad.evidence


def test_claims_grader_reports_empty_corpus(tmp_path):
    (tmp_path / "corpus").mkdir()
    (tmp_path / "brief.md").write_text("something [x]")
    result = grade_claims(GraderSpec("claims", {
        "path": "brief.md", "corpus": "corpus"}), _ctx(tmp_path))
    assert not result.passed
    assert "empty" in result.evidence


# --- checklist ------------------------------------------------------------


def test_checklist_grader_sections_and_length(tmp_path):
    (tmp_path / "brief.md").write_text(
        "# Summary\n- point one\n- point two\n\n# Risks\n- r\n")
    ok = grade_checklist(GraderSpec("checklist", {
        "path": "brief.md", "required_sections": ["Summary", "Risks"],
        "min_items": 2, "max_words": 100}), _ctx(tmp_path))
    assert ok.passed
    bad = grade_checklist(GraderSpec("checklist", {
        "path": "brief.md", "required_sections": ["Recommendation"]}),
        _ctx(tmp_path))
    assert not bad.passed
    assert "Recommendation" in bad.evidence


def test_checklist_grader_enforces_a_word_floor(tmp_path):
    (tmp_path / "brief.md").write_text("# Summary\ntoo short\n")
    result = grade_checklist(GraderSpec("checklist", {
        "path": "brief.md", "min_words": 100}), _ctx(tmp_path))
    assert not result.passed


# --- recompute ------------------------------------------------------------


def test_recompute_grader_independent_arithmetic(tmp_path):
    (tmp_path / "model.json").write_text(json.dumps(
        {"npv": 1000.0, "irr": 0.12}))
    ok = grade_recompute(GraderSpec("recompute", {
        "path": "model.json", "fields": {"npv": 1000.0, "irr": 0.12}}),
        _ctx(tmp_path))
    assert ok.passed

    bad = grade_recompute(GraderSpec("recompute", {
        "path": "model.json", "fields": {"npv": 999.0}}), _ctx(tmp_path))
    assert not bad.passed
    assert "!= expected" in bad.evidence


def test_recompute_grader_tolerance(tmp_path):
    (tmp_path / "m.json").write_text(json.dumps({"x": 1.0005}))
    assert grade_recompute(GraderSpec("recompute", {
        "path": "m.json", "fields": {"x": 1.0}, "tolerance": 0.001}),
        _ctx(tmp_path)).passed
    assert not grade_recompute(GraderSpec("recompute", {
        "path": "m.json", "fields": {"x": 1.0}, "tolerance": 1e-9}),
        _ctx(tmp_path)).passed


def test_recompute_grader_rejects_non_numeric(tmp_path):
    (tmp_path / "m.json").write_text(json.dumps({"x": "not a number"}))
    result = grade_recompute(GraderSpec("recompute", {
        "path": "m.json", "fields": {"x": 1}}), _ctx(tmp_path))
    assert not result.passed
    assert "expected a number" in result.evidence


def test_recompute_grader_rejects_bool_as_number(tmp_path):
    (tmp_path / "m.json").write_text(json.dumps({"x": True}))
    assert not grade_recompute(GraderSpec("recompute", {
        "path": "m.json", "fields": {"x": 1}}), _ctx(tmp_path)).passed


def test_recompute_grader_needs_fields(tmp_path):
    (tmp_path / "m.json").write_text("{}")
    with pytest.raises(Exception):
        grade_recompute(GraderSpec("recompute", {"path": "m.json"}),
                        _ctx(tmp_path))


# --- perf and registry ----------------------------------------------------


def test_perf_grader_claims_nothing():
    result = grade_perf(GraderSpec("perf", {}), GradeContext(Path("/tmp")))
    assert result.passed
    assert "decides nothing" in result.limitation


def test_every_declared_grader_type_has_an_implementation():
    from cupola.suites import GRADER_TYPES
    assert set(GRADER_TYPES) == set(GRADERS)


def test_unknown_grader_type_fails_rather_than_passing(tmp_path):
    results = grade([GraderSpec("astrology", {})], _ctx(tmp_path))
    assert not results[0].passed
    assert "unknown grader type" in results[0].evidence


def test_overall_requires_every_grader(tmp_path):
    (tmp_path / "ok.txt").write_text("fine")
    results = grade([GraderSpec("claims", {"path": "ok.txt",
                                           "must_contain": ["fine"]}),
                     GraderSpec("claims", {"path": "ok.txt",
                                           "must_contain": ["absent"]})],
                    _ctx(tmp_path))
    assert not overall(results)
    assert overall(results[:1])
    assert not overall([]), "no graders must not mean success"


def test_every_result_carries_its_limitation(tmp_path):
    (tmp_path / "ok.txt").write_text("fine")
    results = grade([GraderSpec("claims", {"path": "ok.txt"})],
                    _ctx(tmp_path))
    assert results[0].limitation, "a grader must state what it cannot prove"


# --- harness --------------------------------------------------------------


def test_system_validate_requires_a_prompt_placeholder():
    system = System(name="s", command=["dsh", "--profile", "bench"])
    assert any("must reference the task prompt" in e
               for e in system.validate())
    system.command.append("{prompt}")
    assert system.validate() == []


def test_system_validate_transport_and_command():
    assert any("transport" in e
               for e in System(name="s", transport="carrier-pigeon").validate())
    assert any("needs a command" in e
               for e in System(name="s").validate())
    http = System(name="h", transport=TRANSPORT_HTTP)
    assert any("base_url" in e for e in http.validate())
    http.base_url = "http://127.0.0.1:8080/v1"
    assert http.validate() == []


def test_build_argv_interpolates_every_placeholder(tmp_path):
    system = System(name="s", command=["dsh", "--profile", "{profile}",
                                       "--model", "{model}", "{prompt}"],
                    profile="bench", model="qwen")
    argv = build_argv(system, "do the thing", tmp_path)
    assert argv == ["dsh", "--profile", "bench", "--model", "qwen",
                    "do the thing"]


def test_build_argv_passes_prompt_as_one_token(tmp_path):
    system = System(name="s", command=["dsh", "{prompt}"])
    prompt = "line one; rm -rf / && echo hi\nline two"
    argv = build_argv(system, prompt, tmp_path)
    assert argv[-1] == prompt
    assert len(argv) == 2, "the prompt must never be split by a shell"


def test_build_argv_rejects_an_invalid_system(tmp_path):
    with pytest.raises(ValueError):
        build_argv(System(name="s"), "prompt", tmp_path)


def test_ponytail_arms_differ_only_by_the_overlay(tmp_path):
    overlay = tmp_path / "ponytail-off.yml"
    on = ponytail_arm(PONYTAIL_ON)
    off = ponytail_arm(PONYTAIL_OFF, overlay=overlay)
    assert "--patch" not in on.command
    assert "--patch" in off.command
    stripped = [token for token in off.command if str(overlay) not in token]
    assert off.command.index("--patch") == on.command.index("{prompt}")
    assert stripped[0] == on.command[0]
    assert off.validate() == [] and on.validate() == []
    assert "disabled" in off.notes and "enabled" in on.notes


def test_resolved_binary_finds_python():
    system = System(name="s", command=[sys.executable, "{prompt}"])
    assert resolved_binary(system) is not None
    assert resolved_binary(System(name="http", transport=TRANSPORT_HTTP)) is None


def test_system_round_trips_through_dict():
    raw = {"name": "s", "transport": TRANSPORT_PROCESS,
           "command": ["dsh", "{prompt}"], "env": {"A": "1"},
           "profile": "bench", "model_route": "local"}
    system = system_from_dict(raw)
    assert system.name == "s"
    assert system.env == {"A": "1"}
    assert system.to_dict()["env_keys"] == ["A"]
    assert system.to_dict()["command"] == ["dsh", "{prompt}"]
    assert TRANSPORT_PROCESS in system.to_dict()["transport"]
