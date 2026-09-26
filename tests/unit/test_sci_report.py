"""Carbon metrics and provenance in benchmark reports."""

from __future__ import annotations

from types import SimpleNamespace

from cupola.report import (
    derive_metrics,
    format_report,
    validate_report,
)
from cupola.runner import TaskOutcome

BASE_METRICS = {"comparable_runs": 6, "verified": 6, "tokens_total": 6000,
                "model_calls_total": 6, "unsafe_executions": 0,
                "prohibited_action_attempts": 0, "reproducibility_violations": 0,
                "verified_completion_rate": 1.0}


def _accounting(**extra):
    config = dict(emissions_factor_kgco2e_per_kwh=0.4,
                  emissions_factor_source="user-configured",
                  emissions_factor_version="2024-01",
                  emissions_intensity_basis="location",
                  emissions_region="GB",
                  allow_assumed_power=False,
                  embodied_method="share_of_operational",
                  embodied_share_of_operational=0.12,
                  embodied_total_kgco2e=0.0,
                  embodied_lifetime_hours=0.0)
    config.update(extra)
    return SimpleNamespace(**config)


def _outcome(status="verified", tier=3, kwh=0.01, tokens=1000,
             wall_seconds=10.0, carbon=None):
    outcome = TaskOutcome(task_id=f"t-{tier}-{kwh}", system="stub",
                          status=status, wall_seconds=wall_seconds,
                          usage={"total_tokens": tokens})
    outcome.energy = {"tier": tier, "energy_kwh": kwh, "quality": "ok"}
    if carbon is not None:
        outcome.carbon = carbon
    return outcome


def test_carbon_metrics_from_energy_bearing_runs():
    outcomes = [_outcome() for _ in range(6)]
    metrics = derive_metrics(outcomes, _accounting())
    # 6 x 0.01 kWh x 0.4 kg/kWh = 0.024 kg -> 24 g operational
    assert metrics["energy_kwh_total"] == 0.06
    assert metrics["operational_gco2e_total"] == 24.0
    assert metrics["embodied_gco2e_total"] == 2.88
    assert metrics["sci_gco2e_per_verified_task"] == 4.48
    assert metrics["sci_gco2e_per_1k_tokens"] == 4.48
    assert metrics["energy_runs"] == 6
    assert metrics["carbon"]["energy_quality"] == "estimated"
    assert metrics["carbon"]["tiers"] == {"3": 6}
    assert metrics["carbon"]["intensity"]["source"] == "user-configured"


def test_without_accounting_sci_is_absent_with_a_reason():
    metrics = derive_metrics([_outcome() for _ in range(6)], None)
    assert metrics["sci_gco2e_per_verified_task"] is None
    assert metrics["carbon"]["reason"] == "accounting_unconfigured"
    assert metrics["carbon"]["principles"]


def test_unavailable_energy_is_never_zero():
    outcomes = [_outcome(tier=0, kwh=None) for _ in range(6)]
    metrics = derive_metrics(outcomes, _accounting())
    assert metrics["energy_kwh_total"] is None
    assert metrics["sci_gco2e_per_verified_task"] is None
    assert metrics["carbon"]["reason"] == "energy_unavailable"


def test_assumed_power_is_excluded_unless_allowed():
    outcomes = [_outcome(tier=4, kwh=0.01) for _ in range(6)]
    blocked = derive_metrics(outcomes, _accounting())
    assert blocked["sci_gco2e_per_verified_task"] is None
    # The reason names the rule that excluded the runs, not "no energy".
    assert blocked["carbon"]["reason"] == "assumed_power_not_allowed"

    allowed = derive_metrics(outcomes, _accounting(allow_assumed_power=True))
    assert allowed["sci_gco2e_per_verified_task"] == 4.48
    assert allowed["carbon"]["energy_quality"] == "assumed"


def test_measured_energy_earns_the_principle():
    outcomes = [_outcome(tier=1, kwh=0.01) for _ in range(6)]
    metrics = derive_metrics(outcomes, _accounting())
    by_id = {p["id"]: p for p in metrics["carbon"]["principles"]}
    assert by_id["energy_measured"]["status"] == "pass"
    assert metrics["carbon"]["energy_quality"] == "measured"


def test_per_run_intensity_overrides_the_static_factor():
    carbon = {"intensity": {"kgco2e_per_kwh": 0.2, "source": "neso-gb",
                            "version": "moderate", "basis": "location",
                            "resolution_s": 1800, "matched": "hourly_weighted",
                            "coverage": 1.0},
              "detail": {"series_sha256": "deadbeef"}}
    outcomes = [_outcome(carbon=carbon) for _ in range(6)]
    metrics = derive_metrics(outcomes, _accounting())
    # 0.06 kWh x 0.2 = 0.012 kg -> 12 g operational, + 12% embodied
    assert metrics["operational_gco2e_total"] == 12.0
    assert metrics["carbon"]["intensity"]["source"] == "neso-gb"
    assert metrics["carbon"]["intensity"]["time_resolved_runs"] == 6
    assert metrics["carbon"]["intensity"]["series_sha256"] == "deadbeef"
    by_id = {p["id"]: p for p in metrics["carbon"]["principles"]}
    assert by_id["hourly_matched"]["status"] == "pass"


def test_limitations_state_how_much_is_measurement():
    outcomes = [_outcome(tier=3), _outcome(tier=0, kwh=None)]
    outcomes[0].grades = []
    from cupola.report import _limitations

    limits = _limitations(outcomes)
    assert any("integrated from GPU power" in limit for limit in limits)
    assert any("never as zero" in limit for limit in limits)


def test_validate_report_accepts_both_shapes():
    modern = dict(BASE_METRICS)
    modern["carbon"] = {"principles": []}
    modern["sci_gco2e_per_verified_task"] = 4.48
    base = {"report_version": 1, "suite_id": "s", "suite_fingerprint": "f",
            "generated_at": "now", "systems": [], "runs": [], "limitations": [],
            "metrics": modern, "environment": {}}
    assert validate_report(base) == []

    legacy = {**base, "metrics": dict(BASE_METRICS)}
    assert validate_report(legacy) == []

    negative = {**base, "metrics": {**modern,
                                    "sci_gco2e_per_verified_task": -1.0}}
    assert any("sci_gco2e_per_verified_task" in error
               for error in validate_report(negative))

    bad_principles = {**base, "metrics": {**modern,
                                          "carbon": {"principles": "nope"}}}
    assert any("principles" in error
               for error in validate_report(bad_principles))


def test_format_report_includes_the_carbon_section():
    metrics = derive_metrics([_outcome() for _ in range(6)], _accounting())
    text = format_report({
        "suite_id": "s", "suite_fingerprint": "f", "generated_at": "now",
        "systems": [], "runs": [], "environment": {}, "metrics": metrics})
    assert "SCI: 4.48 gCO2e per verified task" in text
    assert "I: 0.4 kgCO2e/kWh" in text
    assert "Green software principles:" in text


def test_format_report_states_unavailability():
    metrics = derive_metrics([_outcome(tier=0, kwh=None)], None)
    text = format_report({
        "suite_id": "s", "suite_fingerprint": "f", "generated_at": "now",
        "systems": [], "runs": [], "environment": {}, "metrics": metrics})
    assert "SCI: unavailable (accounting_unconfigured)" in text
