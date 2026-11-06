"""Carbon in comparisons: reported, never a gate, never silently compared."""

from __future__ import annotations

import json
from pathlib import Path as _Path

from cupola.compare import (
    VERDICT_INSUFFICIENT,
    VERDICT_PASS,
    attribute_marginal,
    compare_pair,
    format_attribution,
)


def _report(sci, runs=6, rate=1.0, source="user-configured",
            basis="location", resolutions=(0,), method="share_of_operational",
            fingerprint="fp-1"):
    return {
        "suite_fingerprint": fingerprint,
        "metrics": {
            "comparable_runs": runs,
            "verified_completion_rate": rate,
            "sci_gco2e_per_verified_task": sci,
            "tokens_per_success": 100,
            "carbon": {
                "intensity": {"source": source, "basis": basis,
                              "resolutions": list(resolutions)},
                "embodied": {"method": method},
            },
        },
    }


def test_carbon_delta_is_reported_with_direction():
    comparison = compare_pair(_report(10.0), _report(8.0), "minder vs none")
    entry = comparison.deltas["sci_gco2e_per_verified_task"]
    assert entry["baseline"] == 10.0
    assert entry["candidate"] == 8.0
    assert entry["delta"] == -2.0
    assert entry["better"] is True
    assert comparison.carbon["helped"] is True


def test_a_worse_candidate_is_a_negative_result_not_an_error():
    comparison = compare_pair(_report(10.0), _report(12.0), "minder vs none")
    assert comparison.carbon["helped"] is False
    assert comparison.verdict == VERDICT_PASS


def test_carbon_never_flips_the_verdict():
    # A candidate that emits four times as much but completes the same work is
    # still a PASS on the protected metrics: an estimate is not a gate.
    comparison = compare_pair(_report(10.0), _report(40.0))
    assert comparison.verdict == VERDICT_PASS
    assert comparison.carbon["sci_delta_gco2e_per_verified_task"] == 30.0


def test_a_completion_drop_invalidates_the_carbon_win():
    comparison = compare_pair(_report(10.0), _report(1.0, rate=0.5))
    assert comparison.carbon["completion_dropped"] is True
    assert comparison.carbon["helped"] is False


def test_different_methodology_marks_the_delta_not_comparable():
    comparison = compare_pair(
        _report(10.0), _report(8.0, source="neso-gb", basis="marginal",
                               resolutions=(1800,)))
    entry = comparison.deltas["sci_gco2e_per_verified_task"]
    assert entry["comparable"] is False
    assert comparison.carbon["comparable"] is False
    assert comparison.carbon["helped"] == "unknown"
    assert any("methodology differs" in reason for reason in comparison.reasons)
    # the tokens/completion verdict is untouched
    assert comparison.verdict == VERDICT_PASS


def test_different_embodied_method_is_a_mismatch():
    comparison = compare_pair(_report(10.0),
                              _report(8.0, method="amortised"))
    assert comparison.carbon["comparable"] is False


def test_too_few_runs_means_unknown():
    comparison = compare_pair(_report(10.0, runs=2), _report(8.0, runs=2))
    assert comparison.verdict == VERDICT_INSUFFICIENT
    assert comparison.carbon["helped"] == "unknown"


def test_missing_sci_means_unknown():
    comparison = compare_pair(_report(10.0), _report(None))
    assert comparison.carbon["sci_delta_gco2e_per_verified_task"] is None
    assert comparison.carbon["helped"] == "unknown"


def test_fingerprint_mismatch_still_reports_no_deltas():
    from cupola.compare import VERDICT_NON_COMPARABLE

    comparison = compare_pair(_report(10.0), _report(8.0, fingerprint="fp-2"))
    assert comparison.verdict == VERDICT_NON_COMPARABLE
    assert comparison.deltas == {}


def test_marginal_attribution_prints_the_carbon_answer():
    attribution = attribute_marginal(_report(10.0), {"minder": _report(6.0)})
    text = format_attribution(attribution.to_dict())
    assert "sci_gco2e_per_verified_task: 10.0 -> 6.0 (better)" in text
    assert "carbon: helping - SCI delta -4.0 gCO2e per verified task" in text


def test_attribution_prints_unknown_with_a_reason():
    attribution = attribute_marginal(_report(10.0, runs=2),
                                     {"minder": _report(6.0, runs=2)})
    text = format_attribution(attribution.to_dict())
    assert "carbon: unknown" in text


# --- the enriched green-evidence block ------------------------------------


def test_carbon_efficiency_can_actually_be_earned():
    baseline = _report(10.0)
    candidate = _report(6.0, source="user-configured")
    candidate["metrics"]["carbon"]["principles"] = []
    comparison = compare_pair(baseline, candidate, "minder vs none")
    by_id = {p["id"]: p for p in comparison.principles}
    assert by_id["carbon_efficiency_proven"]["status"] == "pass"
    assert "SCI delta" in by_id["carbon_efficiency_proven"]["evidence"]


def test_carbon_efficiency_stays_unproven_when_methodology_differs():
    comparison = compare_pair(_report(10.0),
                              _report(6.0, source="neso-gb",
                                      basis="marginal", resolutions=(1800,)))
    by_id = {p["id"]: p for p in comparison.principles}
    assert by_id["carbon_efficiency_proven"]["status"] == "unproven"


def test_carbon_efficiency_stays_unproven_when_the_candidate_loses():
    comparison = compare_pair(_report(10.0), _report(12.0))
    by_id = {p["id"]: p for p in comparison.principles}
    assert by_id["carbon_efficiency_proven"]["status"] == "unproven"


def test_principles_are_absent_without_a_carbon_block():
    bare = {"suite_fingerprint": "fp-1",
            "metrics": {"comparable_runs": 6, "verified_completion_rate": 1.0,
                        "sci_gco2e_per_verified_task": None}}
    comparison = compare_pair(bare, dict(bare))
    assert comparison.principles == []


def test_format_attribution_summarises_the_evidence():
    attribution = attribute_marginal(_report(10.0), {"minder": _report(6.0)})
    text = format_attribution(attribution.to_dict())
    assert "green evidence:" in text
    assert "carbon_efficiency_proven" in text




# --- the real command, over real reports ----------------------------------

BENCH_ROOT = _Path(__file__).resolve().parents[2] / "benchmarks"


def _accounting():
    from types import SimpleNamespace

    return SimpleNamespace(
        emissions_factor_kgco2e_per_kwh=0.4,
        emissions_factor_source="user-configured",
        emissions_factor_version="2024-01",
        emissions_intensity_basis="location", emissions_region="GB",
        allow_assumed_power=False, embodied_method="share_of_operational",
        embodied_share_of_operational=0.12, embodied_total_kgco2e=0.0,
        embodied_lifetime_hours=0.0)


def _write_report(path, kwh):
    """A six-run report whose runs each measured ``kwh``."""
    from cupola.report import build_report
    from cupola.runner import TaskOutcome
    from cupola.suites import load_suite

    suite = load_suite("coding-core-v2", BENCH_ROOT)
    outcomes = []
    for index in range(6):
        outcome = TaskOutcome(task_id=f"t{index}", system="stub",
                              status="verified", wall_seconds=10.0,
                              usage={"total_tokens": 800})
        outcome.energy = {"tier": 3, "energy_kwh": kwh, "quality": "ok"}
        outcomes.append(outcome)
    build_report(suite, outcomes, None, accounting=_accounting()).write(path)


def _cli_args(baseline, candidate, as_json):
    import argparse

    return argparse.Namespace(baseline=str(baseline), candidate=str(candidate),
                              attribute="marginal",
                              arm=[f"minder={candidate}"], json=as_json)


def test_cli_compare_end_to_end_reports_carbon(tmp_path, capsys):
    from cupola.cli import cmd_bench_compare

    baseline, candidate = tmp_path / "a.json", tmp_path / "b.json"
    _write_report(baseline, 0.020)
    _write_report(candidate, 0.011)

    assert cmd_bench_compare(_cli_args(baseline, candidate, True)) == 0
    payload = json.loads(capsys.readouterr().out)
    comparison = payload["comparisons"][0]
    assert comparison["verdict"] == "PASS"
    assert comparison["carbon"]["helped"] is True
    assert comparison["carbon"]["sci_delta_gco2e_per_verified_task"] < 0
    by_id = {p["id"]: p for p in comparison["principles"]}
    assert by_id["carbon_efficiency_proven"]["status"] == "pass"


def test_cli_compare_human_output_carries_the_carbon_answer(tmp_path, capsys):
    from cupola.cli import cmd_bench_compare

    baseline, candidate = tmp_path / "a.json", tmp_path / "b.json"
    _write_report(baseline, 0.020)
    _write_report(candidate, 0.011)

    assert cmd_bench_compare(_cli_args(baseline, candidate, False)) == 0
    text = capsys.readouterr().out
    assert "carbon: helping - SCI delta" in text
    assert "green evidence:" in text
    assert "carbon_efficiency_proven" in text
