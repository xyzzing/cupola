"""Tests for report comparison and lane attribution.

Verdict precedence matters more than the arithmetic: a safety regression must
fail at any sample size, and a tiny sample must never be reported as a result.
Attribution must also refuse an expensive schedule instead of running it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cupola.cli import main
from cupola.compare import (
    MAX_INTERACTION_LANES,
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT,
    VERDICT_NON_COMPARABLE,
    VERDICT_PASS,
    CompareError,
    attribute_interaction,
    attribute_marginal,
    attribute_total,
    compare_pair,
    format_attribution,
    load_reports,
)

FP = "a" * 64


def _report(rate: float, runs: int = 10, tokens: int = 1000,
            fingerprint: str = FP, unsafe: int = 0,
            tasks: dict | None = None) -> dict:
    """A minimal report shaped like report.py produces."""
    task_runs = []
    verified = int(rate * runs)
    for index in range(runs):
        status = "verified" if index < verified else "failed"
        task_runs.append({
            "task_id": f"t{index}",
            "status": status,
            "usage": {"total_tokens": tokens // max(runs, 1)},
        })
    if tasks is not None:
        task_runs = [{"task_id": k, "status": v,
                      "usage": {"total_tokens": 100}}
                     for k, v in tasks.items()]
    return {
        "report_version": 1,
        "suite_id": "s",
        "suite_fingerprint": fingerprint,
        "metrics": {
            "comparable_runs": runs,
            "verified": verified,
            "verified_completion_rate": rate,
            "tokens_total": tokens,
            "tokens_per_success": tokens / verified if verified else None,
            "tokens_per_verified_task": tokens / verified if verified else None,
            "wall_seconds_p50": 10.0,
            "unsafe_executions": unsafe,
            "prohibited_action_attempts": 0,
            "reproducibility_violations": 0,
        },
        "runs": task_runs,
        "limitations": [],
    }


# --- verdict precedence ---------------------------------------------------


def test_fingerprint_mismatch_is_not_comparable():
    result = compare_pair(_report(0.8), _report(0.8, fingerprint="b" * 64))
    assert result.verdict == VERDICT_NON_COMPARABLE
    assert "suite_fingerprint mismatch" in result.reasons[0]


def test_safety_regression_fails_at_any_sample_size():
    """One unsafe execution beats a large, otherwise-good sample."""
    result = compare_pair(_report(0.5, runs=200), _report(0.99, runs=200,
                                                          unsafe=1))
    assert result.verdict == VERDICT_FAIL
    assert any("absolute regression" in reason for reason in result.reasons)


def test_small_sample_is_insufficient_not_a_result():
    result = compare_pair(_report(0.9, runs=2), _report(0.5, runs=2))
    assert result.verdict == VERDICT_INSUFFICIENT
    assert "comparable runs" in result.reasons[0]


def test_completion_drop_beyond_threshold_fails():
    result = compare_pair(_report(0.90, runs=20), _report(0.80, runs=20))
    assert result.verdict == VERDICT_FAIL
    assert any("dropped" in reason for reason in result.reasons)


def test_drop_within_threshold_passes():
    result = compare_pair(_report(0.90, runs=20), _report(0.87, runs=20))
    assert result.verdict == VERDICT_PASS


def test_improvement_passes():
    result = compare_pair(_report(0.70, runs=20), _report(0.95, runs=20))
    assert result.verdict == VERDICT_PASS


def test_equal_reports_pass():
    result = compare_pair(_report(0.8), _report(0.8))
    assert result.verdict == VERDICT_PASS
    assert "no protected metric regressed" in result.reasons


def test_safety_is_checked_before_sample_size():
    """A tiny sample must not excuse an unsafe execution."""
    result = compare_pair(_report(0.5, runs=1), _report(0.5, runs=1, unsafe=3))
    assert result.verdict == VERDICT_FAIL


# --- deltas and pairing ---------------------------------------------------


def test_deltas_mark_direction_per_metric():
    result = compare_pair(_report(0.9, tokens=1000), _report(0.9, tokens=500))
    tokens = result.deltas["tokens_total"]
    assert tokens["baseline"] == 1000
    assert tokens["candidate"] == 500
    assert tokens["better"] is True, "fewer tokens is better"

    rate = result.deltas["verified_completion_rate"]
    assert rate["better"] is False, "an equal rate is not an improvement"


def test_paired_summary_counts_per_task_changes():
    baseline = _report(0.5, runs=0, tasks={"a": "verified", "b": "failed",
                                           "c": "verified"})
    candidate = _report(0.5, runs=0, tasks={"a": "verified", "b": "verified",
                                            "c": "failed"})
    result = compare_pair(baseline, candidate)
    paired = result.paired
    assert paired["shared_tasks"] == 3
    assert paired["improved"] == 1
    assert paired["regressed"] == 1
    assert paired["unchanged"] == 1


def test_paired_reports_no_shared_tasks():
    baseline = _report(0.5, runs=0, tasks={"a": "verified"})
    candidate = _report(0.5, runs=0, tasks={"z": "verified"})
    assert compare_pair(baseline, candidate).paired["shared_tasks"] == 0


# --- attribution ----------------------------------------------------------


def test_total_attribution_compares_stack_against_none():
    result = attribute_total(_report(0.5), _report(0.9))
    assert result.mode == "total"
    assert len(result.comparisons) == 1
    assert "whole stack" in " ".join(result.notes)


def test_marginal_attribution_gives_one_comparison_per_lane():
    arms = {"ponytail": _report(0.9), "housekeeper": _report(0.4)}
    result = attribute_marginal(_report(0.5), arms)
    assert result.mode == "marginal"
    assert len(result.comparisons) == 2
    verdicts = {c.label: c.verdict for c in result.comparisons}
    assert verdicts["ponytail vs none"] == VERDICT_PASS
    assert verdicts["housekeeper vs none"] == VERDICT_FAIL


def test_interaction_compares_group_against_each_member():
    members = {"a": _report(0.5), "b": _report(0.5)}
    result = attribute_interaction(members, group=_report(0.95))
    assert result.mode == "interaction"
    assert len(result.comparisons) == 2
    assert all("group vs" in c.label for c in result.comparisons)


def test_interaction_refuses_a_combinatorial_schedule():
    members = {f"lane{i}": _report(0.5)
               for i in range(MAX_INTERACTION_LANES + 1)}
    result = attribute_interaction(members, group=_report(0.5))
    assert result.comparisons == []
    assert "pairwise cap" in result.notes[0]
    assert str(2 ** (MAX_INTERACTION_LANES + 1)) in result.notes[0]


def test_interaction_without_a_group_arm_says_so():
    result = attribute_interaction({"a": _report(0.5), "b": _report(0.5)})
    assert result.comparisons == []
    assert any("no group arm" in note for note in result.notes)


def test_single_member_cannot_show_an_interaction():
    result = attribute_interaction({"a": _report(0.5)}, group=_report(0.5))
    assert any("single member" in note for note in result.notes)


def test_format_attribution_mentions_verdicts_and_notes():
    text = format_attribution(attribute_total(_report(0.5),
                                              _report(0.9)).to_dict())
    assert "Attribution mode: total" in text
    assert "PASS" in text


# --- loading --------------------------------------------------------------


def test_load_reports_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(CompareError):
        load_reports([bad])


def test_compare_pair_rejects_a_report_without_metrics():
    with pytest.raises(CompareError):
        compare_pair({"suite_fingerprint": FP}, {"suite_fingerprint": FP})


# --- CLI ------------------------------------------------------------------


def _write(tmp_path: Path, name: str, report: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(report))
    return path


def test_cli_compare_pass(tmp_path, capsys):
    baseline = _write(tmp_path, "a.json", _report(0.7, runs=20))
    candidate = _write(tmp_path, "b.json", _report(0.9, runs=20))
    rc = main(["bench", "compare", "--baseline", str(baseline),
               "--candidate", str(candidate)])
    assert rc == 0
    assert "PASS" in capsys.readouterr().out


def test_cli_compare_fail_exits_nonzero(tmp_path, capsys):
    baseline = _write(tmp_path, "a.json", _report(0.9, runs=20))
    candidate = _write(tmp_path, "b.json", _report(0.5, runs=20))
    rc = main(["bench", "compare", "--baseline", str(baseline),
               "--candidate", str(candidate)])
    assert rc == 1
    assert "FAIL" in capsys.readouterr().out


def test_cli_compare_insufficient_sample(tmp_path, capsys):
    baseline = _write(tmp_path, "a.json", _report(0.9, runs=1))
    candidate = _write(tmp_path, "b.json", _report(0.9, runs=1))
    rc = main(["bench", "compare", "--baseline", str(baseline),
               "--candidate", str(candidate), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["comparisons"][0]["verdict"] == VERDICT_INSUFFICIENT
    # Inconclusive is not success: a run that proves nothing must not exit 0.
    assert rc == 2


def test_cli_compare_marginal_from_arms(tmp_path, capsys):
    baseline = _write(tmp_path, "base.json", _report(0.5, runs=20))
    arm = _write(tmp_path, "pony.json", _report(0.95, runs=20))
    rc = main(["bench", "compare", "--baseline", str(baseline),
               "--candidate", str(arm), "--attribute", "marginal",
               "--arm", f"ponytail={arm}", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "marginal"
    assert payload["comparisons"][0]["label"] == "ponytail vs none"
    assert rc == 0


def test_cli_compare_marginal_needs_an_arm(tmp_path, capsys):
    baseline = _write(tmp_path, "a.json", _report(0.5))
    rc = main(["bench", "compare", "--baseline", str(baseline),
               "--candidate", str(baseline), "--attribute", "marginal"])
    assert rc == 2
    assert "--arm" in capsys.readouterr().err


def test_cli_compare_rejects_a_malformed_arm(tmp_path, capsys):
    baseline = _write(tmp_path, "a.json", _report(0.5))
    rc = main(["bench", "compare", "--baseline", str(baseline),
               "--candidate", str(baseline), "--attribute", "marginal",
               "--arm", "no-equals-sign"])
    assert rc == 2
    assert "LANE=REPORT" in capsys.readouterr().err


def test_cli_compare_missing_file(tmp_path, capsys):
    rc = main(["bench", "compare", "--baseline", str(tmp_path / "nope.json"),
               "--candidate", str(tmp_path / "nope2.json")])
    assert rc == 1
    assert "cannot read report" in capsys.readouterr().err


def test_cli_compare_non_comparable_exits_inconclusive(tmp_path, capsys):
    baseline = _write(tmp_path, "a.json", _report(0.9, runs=20))
    other = _write(tmp_path, "b.json", _report(0.9, runs=20,
                                               fingerprint="b" * 64))
    rc = main(["bench", "compare", "--baseline", str(baseline),
               "--candidate", str(other), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["comparisons"][0]["verdict"] == VERDICT_NON_COMPARABLE
    assert rc == 2
