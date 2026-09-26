"""SCI arithmetic and its honesty rules.

SCI = (E x I + M) / R. Two properties matter more than the arithmetic, and
both are pinned here: a missing number is never zero, and every number carries
the provenance needed to weigh it.
"""

from __future__ import annotations

import pytest

from cupola.sci import (
    DEFAULT_EMBODIED_SHARE,
    STATUS_PASS,
    STATUS_UNPROVEN,
    CarbonIntensity,
    EmbodiedEmissions,
    FunctionalUnit,
    carbon_principles,
    compute_sci,
    effective_intensity,
    embodied_amortised,
    embodied_share_of_operational,
    energy_quality_for_tier,
)


def _intensity(value: float = 0.4) -> CarbonIntensity:
    return CarbonIntensity(value, source="user-configured", version="2024-01",
                           basis="location")


def _unit(count: float = 10.0, unit: str = "verified_task") -> FunctionalUnit:
    return FunctionalUnit(unit=unit, count=count, label=unit)


def test_sci_formula_matches_hand_computation():
    # (0.1 kWh * 0.4 kg/kWh + 0.0048 kg) = 0.0448 kg = 44.8 g over 10 units.
    result = compute_sci(
        energy_kwh=0.1,
        energy_tier=3,
        intensity=_intensity(),
        embodied=EmbodiedEmissions(0.0048),
        functional_unit=_unit(10.0),
    )
    assert result.sci == pytest.approx(4.48)
    assert result.operational_gco2e == pytest.approx(40.0)
    assert result.embodied_gco2e == pytest.approx(4.8)
    assert result.total_gco2e == pytest.approx(44.8)
    assert result.unit == "verified_task"
    assert result.reason == ""


def test_tier_to_quality_mapping():
    assert energy_quality_for_tier(1) == "measured"
    assert energy_quality_for_tier(2) == "measured"
    assert energy_quality_for_tier(3) == "estimated"
    assert energy_quality_for_tier(4) == "assumed"
    assert energy_quality_for_tier(0) == "unavailable"


def test_missing_energy_is_never_zero():
    result = compute_sci(energy_kwh=None, energy_tier=0, intensity=_intensity())
    assert result.sci is None
    assert result.total_gco2e is None
    assert result.reason == "energy_unavailable"

    zero_kwh = compute_sci(energy_kwh=0.0, energy_tier=0, intensity=_intensity())
    assert zero_kwh.sci is None


def test_assumed_power_is_refused_unless_allowed():
    blocked = compute_sci(energy_kwh=0.5, energy_tier=4, intensity=_intensity(),
                          functional_unit=_unit())
    assert blocked.sci is None
    assert blocked.reason == "assumed_power_not_allowed"

    allowed = compute_sci(energy_kwh=0.5, energy_tier=4, intensity=_intensity(),
                          functional_unit=_unit(1.0), allow_assumed_power=True)
    assert allowed.sci is not None
    assert allowed.energy_quality == "assumed"


def test_missing_intensity_reports_its_reason():
    result = compute_sci(energy_kwh=0.5, energy_tier=2, intensity=None,
                         functional_unit=_unit())
    assert result.sci is None
    assert result.reason == "intensity_unconfigured"


def test_missing_functional_unit_keeps_absolute_totals():
    result = compute_sci(energy_kwh=0.1, energy_tier=2, intensity=_intensity(),
                         embodied=EmbodiedEmissions(0.0048),
                         functional_unit=None)
    assert result.sci is None
    assert result.reason == "functional_unit_unavailable"
    assert result.total_gco2e == pytest.approx(44.8)

    zero_count = compute_sci(energy_kwh=0.1, energy_tier=2,
                             intensity=_intensity(), functional_unit=_unit(0.0))
    assert zero_count.sci is None
    assert zero_count.reason == "functional_unit_unavailable"


def test_unsupplied_embodied_is_recorded_not_hidden():
    result = compute_sci(energy_kwh=0.1, energy_tier=2, intensity=_intensity(),
                         functional_unit=_unit())
    assert result.embodied["method"] == "none"
    assert result.embodied["kgco2e"] == 0.0
    assert "unproven" in result.embodied["assumption"]


def test_embodied_share_heuristic():
    embodied = embodied_share_of_operational(0.5)
    assert embodied.method == "share_of_operational"
    assert embodied.kgco2e == pytest.approx(0.5 * DEFAULT_EMBODIED_SHARE)


def test_embodied_amortised_scales_with_workload_hours():
    embodied = embodied_amortised(1000.0, lifetime_hours=20000.0,
                                  workload_hours=10.0)
    assert embodied.method == "amortised"
    assert embodied.kgco2e == pytest.approx(0.5)


def test_embodied_amortised_rejects_an_impossible_lifetime():
    with pytest.raises(ValueError):
        embodied_amortised(1000.0, lifetime_hours=0.0, workload_hours=1.0)


def test_effective_intensity_is_energy_weighted():
    class Point:
        def __init__(self, start, end, value):
            self.ts_start = start
            self.ts_end = end
            self.kgco2e_per_kwh = value
            self.resolution_s = int(end - start)

    points = [Point(0.0, 3600.0, 0.1), Point(3600.0, 7200.0, 0.9)]
    # Heavy load during the dirty hour: the weighted figure must beat the mean.
    samples = [(0.0, 10.0), (3600.0, 90.0)]
    value, detail = effective_intensity(points, samples, fallback=0.5)
    assert value == pytest.approx((10.0 * 0.1 + 90.0 * 0.9) / 100.0)
    assert detail["matched"] == "hourly_weighted"
    assert detail["coverage"] == pytest.approx(1.0)
    assert detail["mean"] == pytest.approx(0.5)
    assert detail["resolution_s"] == 3600


def test_effective_intensity_reports_uncovered_energy():
    class Point:
        def __init__(self, start, end, value):
            self.ts_start, self.ts_end = start, end
            self.kgco2e_per_kwh = value
            self.resolution_s = 3600

    points = [Point(0.0, 3600.0, 0.2)]
    samples = [(0.0, 10.0), (7200.0, 10.0)]  # second sample is outside the series
    value, detail = effective_intensity(points, samples, fallback=None)
    assert value is not None
    assert detail["coverage"] == pytest.approx(0.5)


def test_effective_intensity_refuses_zero_energy():
    class Point:
        ts_start, ts_end, kgco2e_per_kwh, resolution_s = 0.0, 3600.0, 0.3, 3600

    value, detail = effective_intensity([Point()], [(0.0, 0.0)],
                                        fallback=0.99)
    assert value == 0.99
    assert detail["reason"] == "zero_energy"


def test_effective_intensity_without_samples_uses_the_fallback():
    value, detail = effective_intensity([], [], fallback=0.42)
    assert value == 0.42
    assert detail["matched"] == "flat"
    assert detail["reason"] == "no_power_samples"


# --- principles ------------------------------------------------------------


def _report(**carbon) -> dict:
    return {"metrics": {"carbon": carbon}}


def test_principles_default_to_unproven_not_green():
    principles = carbon_principles({"metrics": {}})
    assert principles
    assert {p["status"] for p in principles} == {STATUS_UNPROVEN}


def test_intensity_declared_needs_source_version_and_basis():
    incomplete = _report(intensity={"source": "static", "version": "",
                                    "basis": "location"})
    by_id = {p["id"]: p for p in carbon_principles(incomplete)}
    assert by_id["intensity_declared"]["status"] == STATUS_UNPROVEN

    complete = _report(intensity={"source": "static", "version": "2024-01",
                                  "basis": "location"})
    by_id = {p["id"]: p for p in carbon_principles(complete)}
    assert by_id["intensity_declared"]["status"] == STATUS_PASS


def test_energy_measured_needs_five_metered_runs():
    few = _report(tiers={"1": 4})
    by_id = {p["id"]: p for p in carbon_principles(few)}
    assert by_id["energy_measured"]["status"] == STATUS_UNPROVEN

    enough = _report(tiers={"1": 3, "2": 2})
    by_id = {p["id"]: p for p in carbon_principles(enough)}
    assert by_id["energy_measured"]["status"] == STATUS_PASS


def test_assumed_power_blocks_the_measured_principle():
    report = _report(tiers={"2": 6, "4": 1})
    by_id = {p["id"]: p for p in carbon_principles(report)}
    assert by_id["energy_measured"]["status"] == STATUS_UNPROVEN
    assert "assumed" in by_id["energy_measured"]["evidence"]


def test_hourly_matched_needs_resolution_and_coverage():
    flat = _report(intensity={"matched": "flat", "resolution_s": 0,
                              "coverage": 0.0})
    by_id = {p["id"]: p for p in carbon_principles(flat)}
    assert by_id["hourly_matched"]["status"] == STATUS_UNPROVEN

    hourly = _report(intensity={"matched": "hourly_weighted",
                                "resolution_s": 1800, "coverage": 0.99})
    by_id = {p["id"]: p for p in carbon_principles(hourly)}
    assert by_id["hourly_matched"]["status"] == STATUS_PASS

    thin = _report(intensity={"matched": "hourly_weighted",
                              "resolution_s": 1800, "coverage": 0.5})
    by_id = {p["id"]: p for p in carbon_principles(thin)}
    assert by_id["hourly_matched"]["status"] == STATUS_UNPROVEN


def test_carbon_awareness_needs_a_marginal_signal():
    assert {p["id"]: p for p in carbon_principles(
        _report(intensity={"basis": "marginal"}))}["carbon_awareness"][
            "status"] == STATUS_PASS
    assert {p["id"]: p for p in carbon_principles(
        _report(intensity={"basis": "location"}))}["carbon_awareness"][
            "status"] == STATUS_UNPROVEN


def test_carbon_efficiency_requires_a_comparable_improvement():
    good = _report(comparison={"comparable": True,
                               "sci_delta_gco2e_per_verified_task": -1.5,
                               "completion_dropped": False})
    by_id = {p["id"]: p for p in carbon_principles(good)}
    assert by_id["carbon_efficiency_proven"]["status"] == STATUS_PASS

    worse = _report(comparison={"comparable": True,
                                "sci_delta_gco2e_per_verified_task": 1.5,
                                "completion_dropped": False})
    by_id = {p["id"]: p for p in carbon_principles(worse)}
    assert by_id["carbon_efficiency_proven"]["status"] == STATUS_UNPROVEN

    dropped = _report(comparison={"comparable": True,
                                  "sci_delta_gco2e_per_verified_task": -1.5,
                                  "completion_dropped": True})
    by_id = {p["id"]: p for p in carbon_principles(dropped)}
    assert by_id["carbon_efficiency_proven"]["status"] == STATUS_UNPROVEN


def test_unit_declared_needs_a_value_and_a_unit():
    reported = {"metrics": {"sci_gco2e_per_verified_task": 4.2,
                            "carbon": {"unit": "verified task"}}}
    by_id = {p["id"]: p for p in carbon_principles(reported)}
    assert by_id["unit_declared"]["status"] == STATUS_PASS


def test_effective_intensity_accepts_an_unsorted_series():
    """Providers return newest-first (Energi Data Service) as often as not."""
    class Point:
        def __init__(self, start, end, value):
            self.ts_start, self.ts_end = start, end
            self.kgco2e_per_kwh = value
            self.resolution_s = int(end - start)

    points = [Point(300, 600, 0.9), Point(0, 300, 0.1), Point(600, 900, 0.5)]
    value, detail = effective_intensity(
        points, [(0.0, 1.0), (300.0, 1.0), (600.0, 1.0)], fallback=None)
    assert value == pytest.approx(0.5)
    assert detail["coverage"] == pytest.approx(1.0)


def test_effective_intensity_resolves_a_full_session_correctly():
    """Session-scale correctness for the indexed lookup.

    The lookup is O(log n) per sample rather than a scan (a 24 h session at
    1 Hz against a 5 min series went from ~1.1 s to ~0.05 s), but a wall-clock
    bound here would be flaky on a loaded machine, so this pins the *result* at
    a size where a quadratic implementation would be painful to run, and the
    indexing itself is asserted by the unsorted-series test above.
    """
    class Point:
        def __init__(self, start, end, value):
            self.ts_start, self.ts_end = start, end
            self.kgco2e_per_kwh = value
            self.resolution_s = 300

    points = [Point(i * 300, (i + 1) * 300, 0.3) for i in range(288)]
    samples = [(float(i), 300.0) for i in range(20_000)]
    value, detail = effective_intensity(points, samples, fallback=0.4)
    assert value == pytest.approx(0.3)
    assert detail["coverage"] == pytest.approx(1.0)
    assert detail["resolution_s"] == 300
