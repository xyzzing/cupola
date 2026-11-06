"""Session SCI, the wall meter, and the benchmark energy window."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from cupola.sensors import SensorReading
from cupola.telemetry import (
    EnergyMeter,
    SentinelSampler,
    _read_wall_counter,
    compute_session_energy,
)


def _reading(ts: str, power_w=None, energy_uj=None) -> SensorReading:
    return SensorReading(ts=ts, source="test", quality="ok", power_w=power_w,
                         energy_uj=energy_uj)


def _accounting(**extra):
    config = dict(
        currency="USD",
        electricity_tariff_per_kwh=None,
        emissions_region="GB",
        emissions_factor_kgco2e_per_kwh=0.4,
        emissions_factor_source="user-configured",
        emissions_factor_version="2024-01",
        emissions_intensity_basis="location",
        allow_assumed_power=False,
        embodied_method="share_of_operational",
        embodied_share_of_operational=0.12,
        embodied_total_kgco2e=0.0,
        embodied_lifetime_hours=0.0,
    )
    config.update(extra)
    return config


def _carbon(**extra):
    config = dict(provider="static", zone="GB", basis="location", file="",
                  file_unit="gco2e_per_kwh", api_key_env="", api_key="",
                  cache_ttl_s=3600.0, timeout_s=5.0, require_hourly=False)
    config.update(extra)
    return config


# --- wall meter ------------------------------------------------------------


def test_wall_counter_units_are_declared(tmp_path):
    counter = tmp_path / "meter"
    counter.write_text("1500\n")
    assert _read_wall_counter(str(counter), "wh") == pytest.approx(1.5)
    assert _read_wall_counter(str(counter), "kwh") == pytest.approx(1500.0)
    assert _read_wall_counter(str(counter), "j") == pytest.approx(1500 / 3.6e6)


def test_wall_counter_rejects_unknown_units_and_missing_files(tmp_path):
    counter = tmp_path / "meter"
    counter.write_text("1\n")
    assert _read_wall_counter(str(counter), "bananas") is None
    assert _read_wall_counter(str(tmp_path / "nope"), "kwh") is None
    assert _read_wall_counter(None, "kwh") is None


def test_wall_meter_wins_over_every_other_tier():
    result = compute_session_energy(
        samples=[_reading("2026-01-01T00:00:00+00:00", power_w=300.0)],
        first_energy_uj=0, last_energy_uj=10 ** 12,
        duration_s=10.0, sample_hz=1.0, wall_delta_kwh=0.25)
    assert result.tier == 1
    assert result.energy_kwh == pytest.approx(0.25)
    assert result.quality == "ok"


def test_tier_precedence_without_a_wall_meter():
    from cupola.telemetry import compute_energy_tier2

    counter = compute_session_energy(
        samples=[_reading("2026-01-01T00:00:00+00:00", power_w=300.0)],
        first_energy_uj=0, last_energy_uj=3_600_000_000, duration_s=1.0,
        sample_hz=1.0)
    assert counter.tier == 2
    expected = compute_energy_tier2(0, 3_600_000_000, 1.0)
    assert counter.energy_kwh == pytest.approx(expected.energy_kwh)

    integrated = compute_session_energy(
        samples=[_reading("2026-01-01T00:00:00+00:00", power_w=3600000.0)],
        duration_s=1.0, sample_hz=1.0)
    assert integrated.tier == 3

    assumed = compute_session_energy(samples=[], duration_s=3600.0,
                                     assume_power_w=100.0)
    assert assumed.tier == 4
    assert assumed.energy_kwh == pytest.approx(0.1)

    nothing = compute_session_energy(samples=[])
    assert nothing.tier == 0
    assert nothing.energy_kwh is None


# --- session summary -------------------------------------------------------


def _sampler(tmp_path, accounting=None, carbon=None, samples=None):
    sampler = SentinelSampler(
        instance_uuid="uuid-1", profile="coding", pid=1, start_time=0,
        state_dir=tmp_path, sample_hz=1.0,
        accounting=accounting if accounting is not None else _accounting(),
        carbon=carbon if carbon is not None else _carbon(),
        cache_dir=tmp_path / "cache",
    )
    seeded = samples if samples is not None else [
        _reading("2026-01-01T00:00:00+00:00", power_w=300.0),
        _reading("2026-01-01T00:00:01+00:00", power_w=300.0),
    ]
    # Feed the meter directly: the accumulation logic under test is its own.
    for reading in seeded:
        sampler._meter._samples.append(reading)
        if reading.power_w is not None:
            sampler._meter._power_sum += reading.power_w
            sampler._meter._power_count += 1
    if seeded:
        sampler._meter._first_energy_uj = 0
        sampler._meter._last_energy_uj = 3_600_000_000  # 0.001 kWh
    return sampler


def test_session_summary_carries_sci_per_session_hour(tmp_path):
    sampler = _sampler(tmp_path)
    sampler._session_start -= 3600.0  # a one-hour session, so a rate is real
    summary = sampler._write_summary()
    payload = summary.to_dict()
    assert payload["sci"] is not None
    sci = payload["sci"]
    assert sci["unit"] == "session_hour"
    assert sci["sci"] is not None
    assert sci["energy_quality"] == "measured"  # tier 2
    # 0.001 kWh * 0.4 kg/kWh * 1.12 (M) * 1000 = 0.448 g over a tiny duration
    assert sci["total_gco2e"] == pytest.approx(0.448, rel=1e-6)
    assert sci["intensity"]["source"] == "user-configured"


def test_session_summary_without_accounting_says_why(tmp_path):
    sampler = _sampler(tmp_path, accounting={})
    payload = sampler._write_summary().to_dict()
    assert payload["sci"] is None
    assert payload["sci_reason"] == "accounting_unconfigured"


def test_session_summary_without_energy_never_reports_zero(tmp_path):
    sampler = _sampler(tmp_path)
    sampler._meter._first_energy_uj = None
    sampler._meter._last_energy_uj = None
    sampler._meter._samples = []
    payload = sampler._write_summary().to_dict()
    assert payload["sci"] is None
    assert payload["sci_reason"] == "energy_unavailable"


def test_assumed_power_is_gated_in_the_session_summary(tmp_path):
    sampler = _sampler(tmp_path, accounting=_accounting(allow_assumed_power=False))
    sampler._meter._first_energy_uj = None
    sampler._meter._last_energy_uj = None
    sampler._meter._samples = []
    sampler._meter.assume_power_w = 100.0
    sampler._session_start -= 60.0
    payload = sampler._write_summary().to_dict()
    assert payload["energy"]["tier"] == 4
    assert payload["sci"] is None
    assert payload["sci_reason"] == "assumed_power_not_allowed"


def test_session_summary_is_written_to_disk(tmp_path):
    sampler = _sampler(tmp_path)
    sampler._write_summary()
    written = list((tmp_path / "telemetry" / "sessions").glob("*.json"))
    assert len(written) == 1


# --- the accumulator -------------------------------------------------------


def test_meter_integrates_power_over_a_window():
    meter = EnergyMeter(sample_hz=50.0,
                        probe=lambda: _reading(
                            "2026-01-01T00:00:00+00:00", power_w=3600.0))
    meter.start()
    time.sleep(0.1)
    meter.halt()
    result = meter.energy()
    assert result.tier == 3
    assert result.energy_kwh is not None and result.energy_kwh > 0
    assert meter.sample_count > 0
    assert meter.avg_power_w == pytest.approx(3600.0)
    assert meter.peak_power_w == pytest.approx(3600.0)


def test_meter_without_power_values_reports_unavailable_not_zero():
    """Samples with no power value are absence, not a zero-kilowatt-hour run."""
    meter = EnergyMeter(sample_hz=50.0,
                        probe=lambda: _reading("2026-01-01T00:00:00+00:00"))
    meter.start()
    time.sleep(0.05)
    meter.halt()
    result = meter.energy()
    assert meter.sample_count > 0
    assert result.energy_kwh is None
    assert result.tier == 0
    assert result.quality == "unavailable"
    assert meter.avg_power_w is None
    assert meter.peak_power_w is None


def test_meter_halt_is_safe_before_start():
    meter = EnergyMeter(probe=lambda: _reading("2026-01-01T00:00:00+00:00"))
    meter.halt()
    meter.halt()
    assert meter.sample_count == 0


def test_meter_reports_probe_failures_to_the_caller():
    seen = []

    def exploding_probe():
        raise RuntimeError("sensor bus gone")

    meter = EnergyMeter(sample_hz=50.0, probe=exploding_probe,
                        on_error=seen.append)
    meter.start()
    time.sleep(0.05)
    meter.halt()
    assert seen and isinstance(seen[0], RuntimeError)
    assert meter.energy().tier == 0


def test_meter_reading_hook_sees_every_sample():
    seen = []
    meter = EnergyMeter(sample_hz=50.0,
                        probe=lambda: _reading("2026-01-01T00:00:00+00:00",
                                               power_w=10.0),
                        on_reading=seen.append)
    meter.start()
    time.sleep(0.05)
    meter.halt()
    assert len(seen) == meter.sample_count


def test_runner_records_energy_and_intensity(tmp_path):
    from cupola.runner import RunnerConfig, TaskOutcome, _close_energy_window
    from cupola.telemetry import EnergyResult

    class StubMeter:
        started_at = 1000.0
        samples = [_reading("2026-01-01T00:00:00+00:00", power_w=300.0)]

        def halt(self):
            pass

        def energy(self, duration_s=None):
            return EnergyResult(energy_kwh=0.01, tier=3,
                                source="sysfs:power1_average", sample_hz=1.0,
                                missing_samples=0,
                                method="rectangular_integration",
                                quality="ok")

    outcome = TaskOutcome(task_id="t", system="stub", status="verified",
                          wall_seconds=10.0)
    config = RunnerConfig(
        accounting=SimpleNamespace(**_accounting()),
        carbon=SimpleNamespace(**_carbon()),
        cache_dir=tmp_path / "cache",
    )
    _close_energy_window(outcome, StubMeter(), config)

    assert outcome.energy["tier"] == 3
    assert outcome.energy["energy_kwh"] == pytest.approx(0.01)
    assert outcome.carbon["intensity"]["kgco2e_per_kwh"] == pytest.approx(0.4)
    assert outcome.carbon["intensity"]["matched"] == "flat"
    # the round trip through the report row keeps both blocks
    row = outcome.to_dict()
    assert row["energy"]["tier"] == 3
    # the fallback's source is named as the accounting factor, and what was
    # asked for is recorded separately — the two used to be conflated
    assert row["carbon"]["detail"]["source"] == "user-configured"
    assert row["carbon"]["detail"]["requested_provider"] == "static"


def test_runner_without_accounting_records_energy_only(tmp_path):
    from cupola.runner import RunnerConfig, TaskOutcome, _close_energy_window
    from cupola.telemetry import EnergyResult

    class StubMeter:
        started_at = 1000.0
        samples = []

        def halt(self):
            pass

        def energy(self, duration_s=None):
            return EnergyResult(energy_kwh=None, tier=0, source="none",
                                sample_hz=1.0, missing_samples=0,
                                method="none", quality="unavailable")

    outcome = TaskOutcome(task_id="t", system="stub", status="verified")
    _close_energy_window(outcome, StubMeter(), RunnerConfig())
    assert outcome.energy["tier"] == 0
    assert outcome.carbon == {}


def test_supervisor_sampler_kwargs_are_accepted(tmp_path):
    """The exact kwargs Supervisor._start_sentinel passes to SentinelSampler.

    Signature drift here would only surface when a real instance starts, so it
    is pinned against the config objects the supervisor actually holds.
    """
    from cupola.config import load_config

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[accounting]\n"
        "emissions_factor_kgco2e_per_kwh = 0.4\n"
        "emissions_factor_source = 'user-configured'\n"
        "emissions_factor_version = '2024-01'\n"
        "\n[carbon]\n"
        "provider = 'neso-gb'\n"
        "zone = 'GB'\n"
        "\n[sentinel]\n"
        "wall_energy_unit = 'wh'\n"
    )
    config = load_config(config_file)
    sampler = SentinelSampler(
        instance_uuid="uuid-2",
        profile="coding",
        pid=123,
        start_time=0,
        state_dir=tmp_path / "state",
        sample_hz=config.sentinel.sample_hz,
        warn_hotspot_c=config.sentinel.warn_hotspot_c,
        critical_hotspot_c=config.sentinel.critical_hotspot_c,
        critical_hold_s=config.sentinel.critical_hold_s,
        auto_stop_on_critical=config.sentinel.auto_stop_on_critical,
        assume_power_w=config.sentinel.assume_power_w,
        wall_energy_path=config.sentinel.wall_energy_path,
        wall_energy_unit=config.sentinel.wall_energy_unit,
        accounting=config.accounting.__dict__,
        carbon=config.carbon.__dict__,
        cache_dir=config.cache_dir,
    )
    assert sampler.carbon["provider"] == "neso-gb"
    # the wiring assertions: constructor args land on the one accumulator
    assert sampler._meter.wall_energy_unit == "wh"
    assert sampler._meter.sample_hz == config.sentinel.sample_hz
    # the dict -> attribute bridge the SCI computation relies on
    from types import SimpleNamespace

    assert SimpleNamespace(**sampler.carbon).zone == "GB"
    assert SimpleNamespace(**sampler.accounting).embodied_method == \
        "share_of_operational"


def test_amortised_without_figures_records_the_substitution():
    from cupola.telemetry import compute_embodied

    configured_but_unusable = SimpleNamespace(
        embodied_method="amortised", embodied_total_kgco2e=0.0,
        embodied_lifetime_hours=0.0, embodied_share_of_operational=0.12,
        emissions_factor_source="user-configured")
    embodied = compute_embodied(
        accounting=configured_but_unusable, operational_kgco2e=0.5,
        workload_hours=1.0)
    assert embodied.method == "share_of_operational"
    assert "amortised" in embodied.assumption
    assert "using the heuristic instead" in embodied.assumption

    usable = SimpleNamespace(
        embodied_method="amortised", embodied_total_kgco2e=1200.0,
        embodied_lifetime_hours=20000.0, embodied_share_of_operational=0.12,
        emissions_factor_source="user-configured")
    amortised = compute_embodied(
        accounting=usable, operational_kgco2e=0.5, workload_hours=10.0)
    assert amortised.method == "amortised"
    assert amortised.kgco2e == pytest.approx(1200.0 * 10.0 / 20000.0)


def test_meter_floors_the_sample_interval():
    """sample_hz = 0 used to divide by zero inside the sampling thread."""
    assert EnergyMeter(sample_hz=0).sample_hz == pytest.approx(0.01)
    assert EnergyMeter(sample_hz=-5).sample_hz == pytest.approx(0.01)
    assert EnergyMeter(sample_hz=4.0).sample_hz == pytest.approx(4.0)


def test_sampler_hook_drives_extrema_and_live_state(tmp_path):
    """The per-reading callback is the refactor's riskiest join.

    Drives the real sampling thread with a cycling probe, so the delegation from
    EnergyMeter back into the session's thermal/live work is exercised end to
    end rather than by poking internals.
    """
    import itertools

    samples = itertools.cycle([
        _reading("2026-01-01T00:00:00+00:00", power_w=100.0),
        _reading("2026-01-01T00:00:01+00:00", power_w=250.0),
    ])
    sampler = SentinelSampler(
        instance_uuid="hook-check", profile="coding", pid=1, start_time=0,
        state_dir=tmp_path, sample_hz=50.0, probe=lambda: next(samples),
        accounting=_accounting(), carbon=_carbon(), cache_dir=tmp_path / "cache")
    sampler.start()
    time.sleep(0.15)
    summary = sampler.stop().to_dict()

    assert summary["sample_count"] > 0
    # the hook ran per reading: live state and power stats were written
    assert (tmp_path / "telemetry" / "current.json").exists()
    assert summary["avg_power_w"] in (100.0, 250.0) or \
        100.0 < summary["avg_power_w"] < 250.0
    assert summary["peak_power_w"] == pytest.approx(250.0)
    assert (tmp_path / "telemetry" / "sessions" / "hook-check.json").exists()


# --- integration behaviour a self-review missed ---------------------------


def _provider_carbon(**extra):
    config = _carbon(provider="neso-gb", zone="GB", **extra)
    return config


def _no_network(monkeypatch):
    import cupola.carbon_intensity as ci

    def boom(url, timeout):
        raise ci.IntensityError("offline")

    monkeypatch.setattr(ci, "_http_get_json", boom)
    return ci


def test_require_hourly_refusal_survives_the_outer_layer(monkeypatch, tmp_path):
    """The inner layer honouring require_hourly is not enough.

    This is the layer that used to substitute the annual factor for a refused
    hourly lookup, defeating the setting one level above the code that
    implements it.
    """
    from types import SimpleNamespace

    _no_network(monkeypatch)
    from cupola.telemetry import build_intensity

    carbon = SimpleNamespace(**_provider_carbon(require_hourly=True))
    accounting = SimpleNamespace(**_accounting())
    intensity, detail = build_intensity(
        carbon=carbon, accounting=accounting, start=0.0, end=3600.0,
        samples=[_reading("2026-01-01T00:00:00+00:00", power_w=100.0)],
        cache_dir=tmp_path)
    assert intensity is None
    assert "intensity_hourly_unavailable" in detail["reason"]


def test_require_hourly_refusal_reaches_the_session_summary(monkeypatch, tmp_path):
    _no_network(monkeypatch)
    sampler = _sampler(tmp_path, carbon=_provider_carbon(require_hourly=True))
    sampler._session_start -= 3600.0
    payload = sampler._write_summary().to_dict()
    assert payload["sci"] is None
    assert "intensity_hourly_unavailable" in payload["sci_reason"]


def test_a_refused_intensity_is_not_substituted_by_the_report(tmp_path):
    """report._intensity_for must not re-defeat the refusal either."""
    from types import SimpleNamespace

    from cupola.report import derive_metrics
    from cupola.runner import TaskOutcome

    outcome = TaskOutcome(task_id="t", system="stub", status="verified",
                          wall_seconds=10.0, usage={"total_tokens": 100})
    outcome.energy = {"tier": 3, "energy_kwh": 0.01, "quality": "ok"}
    # the run recorded that it refused: intensity null, reason present
    outcome.carbon = {"intensity": None,
                      "detail": {"reason": "intensity_hourly_unavailable: offline"}}
    metrics = derive_metrics([outcome], SimpleNamespace(**_accounting()))
    assert metrics["sci_gco2e_per_verified_task"] is None
    assert "intensity_hourly_unavailable" in metrics["carbon"]["reason"]


def test_static_fallback_does_not_wear_the_provider_name(monkeypatch, tmp_path):
    """A resolved series with no usable power samples falls back to static."""
    from types import SimpleNamespace

    from cupola import carbon_intensity as ci
    from cupola.telemetry import build_intensity

    class Point:
        ts_start, ts_end, kgco2e_per_kwh, resolution_s = 0.0, 3600.0, 0.2, 1800

    def payload(url, timeout):
        return {"data": [{"from": "1970-01-01T00:00Z",
                          "to": "1970-01-01T00:30Z",
                          "intensity": {"actual": 200, "index": "low"}}]}

    monkeypatch.setattr(ci, "_http_get_json", payload)
    carbon = SimpleNamespace(**_provider_carbon())
    accounting = SimpleNamespace(**_accounting())
    intensity, detail = build_intensity(
        carbon=carbon, accounting=accounting, start=0.0, end=3600.0,
        samples=[],  # no power readings: the series cannot be matched
        cache_dir=tmp_path)
    assert intensity is not None
    assert intensity.kgco2e_per_kwh == pytest.approx(0.4)      # the static factor
    assert intensity.source == "user-configured"               # not "neso-gb"
    assert intensity.resolution_s == 0
    assert detail["used_fallback"] is True
    assert detail["requested_provider"] == "neso-gb"


def test_non_utf8_series_is_a_reason_not_a_crash(tmp_path):
    from cupola import carbon_intensity as ci

    path = tmp_path / "utf16.csv"
    path.write_bytes("timestamp,intensity\n2026-01-01T00:00:00Z,300\n"
                     .encode("utf-16"))
    carbon = _carbon(provider="file", file=str(path))
    resolved = ci.resolve_intensity(
        carbon=__import__("types").SimpleNamespace(**carbon),
        accounting=__import__("types").SimpleNamespace(**_accounting()),
        start=0.0, end=9_999_999_999.0, cache_dir=tmp_path)
    assert resolved.points == ()
    assert "cannot read" in resolved.reason


def test_sub_minute_window_still_asks_for_a_range(monkeypatch, tmp_path):
    """A 20 s run must not request from == to and silently lose its figure."""
    from types import SimpleNamespace

    from cupola import carbon_intensity as ci

    seen = {}

    def payload(url, timeout):
        seen["url"] = url
        return {"data": [{"from": "2026-01-01T00:00Z", "to": "2026-01-01T00:30Z",
                          "intensity": {"actual": 200}}]}

    monkeypatch.setattr(ci, "_http_get_json", payload)
    import datetime as dt

    start = dt.datetime(2026, 1, 1, 0, 0, 30, tzinfo=dt.timezone.utc).timestamp()
    ci.resolve_intensity(
        carbon=SimpleNamespace(**_provider_carbon()),
        accounting=SimpleNamespace(**_accounting()),
        start=start, end=start + 20, cache_dir=tmp_path)
    # the window sent upstream is widened to whole minutes and is not empty
    assert "00:00Z" in seen["url"]
    assert not seen["url"].rstrip("/").endswith("00:00Z/2026-01-01T00:00Z")


def test_tier2_stuck_counter_is_unavailable_not_zero():
    result = compute_session_energy(
        samples=[], first_energy_uj=500, last_energy_uj=500, duration_s=60.0,
        sample_hz=1.0)
    assert result.energy_kwh is None
    assert result.tier == 0

    reversed_counter = compute_session_energy(
        samples=[], first_energy_uj=500, last_energy_uj=100, duration_s=60.0,
        sample_hz=1.0)
    assert reversed_counter.energy_kwh is None


def test_tier2_stuck_counter_falls_through_to_power_integration():
    result = compute_session_energy(
        samples=[_reading("2026-01-01T00:00:00+00:00", power_w=3600.0)],
        first_energy_uj=500, last_energy_uj=500, duration_s=1.0, sample_hz=1.0)
    assert result.tier == 3
    assert result.energy_kwh is not None


def test_all_missing_power_leaves_no_zero_behind(tmp_path):
    """The sibling co2e_kg field must be absent too, not 0.0."""
    sampler = _sampler(tmp_path)
    sampler._meter._samples = [
        _reading("2026-01-01T00:00:00+00:00"),
        _reading("2026-01-01T00:00:01+00:00"),
    ]
    sampler._meter._first_energy_uj = None
    sampler._meter._last_energy_uj = None
    payload = sampler._write_summary().to_dict()
    assert payload["energy"]["energy_kwh"] is None
    assert payload["co2e_kg"] is None
    assert payload["sci"] is None


def test_short_session_has_no_rate(tmp_path):
    sampler = _sampler(tmp_path)
    payload = sampler._write_summary().to_dict()
    assert payload["sci"] is None
    assert payload["sci_reason"] == "session_too_short"
