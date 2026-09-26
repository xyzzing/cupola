"""Intensity providers: parsing, caching, and failing open."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cupola import carbon_intensity as ci


def _accounting(value=0.4, source="user-configured", version="2024-01",
                basis="location"):
    return SimpleNamespace(
        emissions_factor_kgco2e_per_kwh=value,
        emissions_factor_source=source,
        emissions_factor_version=version,
        emissions_intensity_basis=basis,
        emissions_region="GB",
    )


def _carbon(provider="static", zone="GB", basis="location", **extra):
    config = dict(provider=provider, zone=zone, basis=basis, file="",
                  file_unit="gco2e_per_kwh", api_key_env="", api_key="",
                  cache_ttl_s=3600.0, timeout_s=5.0, require_hourly=False)
    config.update(extra)
    return SimpleNamespace(**config)


NESO_PAYLOAD = {
    "data": [
        {"from": "2026-01-01T00:00Z", "to": "2026-01-01T00:30Z",
         "intensity": {"forecast": 266, "actual": 263, "index": "moderate"}},
        {"from": "2026-01-01T00:30Z", "to": "2026-01-01T01:00Z",
         "intensity": {"forecast": 300, "actual": None, "index": "high"}},
    ]
}

DK_PAYLOAD = {
    "records": [
        {"Minutes5UTC": "2026-01-01T00:00:00", "PriceArea": "DK1",
         "CO2Emission": 120.0},
        {"Minutes5UTC": "2026-01-01T00:05:00", "PriceArea": "DK1",
         "CO2Emission": 130.0},
    ]
}


def test_static_provider_opens_no_socket(monkeypatch, tmp_path):
    def explode(*_args, **_kwargs):
        raise AssertionError("static must not touch the network")

    monkeypatch.setattr(ci, "_http_get_json", explode)
    resolved = ci.resolve_intensity(
        carbon=_carbon(provider="static"), accounting=_accounting(),
        start=0.0, end=3600.0, cache_dir=tmp_path)
    assert resolved.source == "user-configured"
    assert resolved.reason == ""
    assert resolved.points == ()


def test_static_without_a_factor_says_so():
    resolved = ci.resolve_intensity(
        carbon=_carbon(), accounting=_accounting(value=None),
        start=0.0, end=3600.0, cache_dir=None)
    assert resolved.reason == "intensity_unconfigured"


def test_documented_but_unimplemented_provider_is_reported(monkeypatch, tmp_path):
    def explode(*_args, **_kwargs):
        raise AssertionError("an unimplemented provider must not dial out")

    monkeypatch.setattr(ci, "_http_get_json", explode)
    resolved = ci.resolve_intensity(
        carbon=_carbon(provider="electricitymaps"), accounting=_accounting(),
        start=0.0, end=3600.0, cache_dir=tmp_path)
    assert "provider_not_implemented" in resolved.reason
    assert resolved.points == ()


def test_neso_parses_half_hourly_and_converts_grams(monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "_http_get_json", lambda url, timeout: NESO_PAYLOAD)
    resolved = ci.resolve_intensity(
        carbon=_carbon(provider="neso-gb"), accounting=_accounting(),
        start=0.0, end=3600.0, cache_dir=tmp_path)
    assert resolved.time_resolved
    assert resolved.resolution_s == ci.NESO_RESOLUTION_S
    assert resolved.source == "neso-gb"
    # 263 gCO2e/kWh -> 0.263 kgCO2e/kWh
    assert resolved.points[0].kgco2e_per_kwh == pytest.approx(0.263)
    # a null "actual" falls back to the forecast
    assert resolved.points[1].kgco2e_per_kwh == pytest.approx(0.300)


def test_energidataservice_parses_five_minute_records(monkeypatch, tmp_path):
    import datetime as dt

    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp()
    monkeypatch.setattr(ci, "_http_get_json", lambda url, timeout: DK_PAYLOAD)
    resolved = ci.resolve_intensity(
        carbon=_carbon(provider="energidataservice-dk", zone="DK1"),
        accounting=_accounting(), start=start, end=start + 600,
        cache_dir=tmp_path)
    assert resolved.resolution_s == ci.ENERGIDATASERVICE_RESOLUTION_S
    assert resolved.points[0].kgco2e_per_kwh == pytest.approx(0.120)


def test_provider_failure_falls_back_to_the_static_factor(monkeypatch, tmp_path):
    def boom(url, timeout):
        raise ci.IntensityError("429 rate limited")

    monkeypatch.setattr(ci, "_http_get_json", boom)
    resolved = ci.resolve_intensity(
        carbon=_carbon(provider="neso-gb"), accounting=_accounting(value=0.5),
        start=0.0, end=3600.0, cache_dir=tmp_path)
    assert resolved.points == ()
    assert "provider_error_static_fallback" in resolved.reason
    assert "429" in resolved.reason


def test_require_hourly_refuses_the_coarse_fallback(monkeypatch, tmp_path):
    """Provider layer only — the telemetry layer that could defeat it has its
    own test in test_sci_telemetry.py."""
    def boom(url, timeout):
        raise ci.IntensityError("offline")

    monkeypatch.setattr(ci, "_http_get_json", boom)
    resolved = ci.resolve_intensity(
        carbon=_carbon(provider="neso-gb", require_hourly=True),
        accounting=_accounting(value=0.5), start=0.0, end=3600.0,
        cache_dir=tmp_path)
    assert resolved.points == ()
    assert "intensity_hourly_unavailable" in resolved.reason


def test_cache_avoids_a_second_fetch(monkeypatch, tmp_path):
    calls = {"n": 0}

    def counted(url, timeout):
        calls["n"] += 1
        return NESO_PAYLOAD

    monkeypatch.setattr(ci, "_http_get_json", counted)
    first = ci.resolve_intensity(
        carbon=_carbon(provider="neso-gb"), accounting=_accounting(),
        start=0.0, end=3600.0, cache_dir=tmp_path)
    second = ci.resolve_intensity(
        carbon=_carbon(provider="neso-gb"), accounting=_accounting(),
        start=0.0, end=3600.0, cache_dir=tmp_path)
    assert calls["n"] == 1
    assert first.series_sha256 == second.series_sha256
    assert first.series_sha256 != ""


def test_expired_cache_refetches(monkeypatch, tmp_path):
    calls = {"n": 0}

    def counted(url, timeout):
        calls["n"] += 1
        return NESO_PAYLOAD

    monkeypatch.setattr(ci, "_http_get_json", counted)
    carbon = _carbon(provider="neso-gb", cache_ttl_s=10.0)
    ci.resolve_intensity(carbon=carbon, accounting=_accounting(),
                         start=0.0, end=3600.0, cache_dir=tmp_path, now=1000.0)
    ci.resolve_intensity(carbon=carbon, accounting=_accounting(),
                         start=0.0, end=3600.0, cache_dir=tmp_path, now=2000.0)
    assert calls["n"] == 2


def test_stale_cache_is_better_than_no_data(monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "_http_get_json", lambda url, timeout: NESO_PAYLOAD)
    carbon = _carbon(provider="neso-gb", cache_ttl_s=10.0)
    good = ci.resolve_intensity(
        carbon=carbon, accounting=_accounting(), start=0.0, end=3600.0,
        cache_dir=tmp_path, now=1000.0)

    def boom(url, timeout):
        raise ci.IntensityError("network down")

    monkeypatch.setattr(ci, "_http_get_json", boom)
    stale = ci.resolve_intensity(
        carbon=carbon, accounting=_accounting(), start=0.0, end=3600.0,
        cache_dir=tmp_path, now=2000.0)
    assert stale.time_resolved
    assert stale.series_sha256 == good.series_sha256
    assert "provider_error_using_cache" in stale.reason


def test_file_provider_json_grams(tmp_path):
    series = [
        {"from": "2026-01-01T00:00Z", "to": "2026-01-01T01:00Z",
         "intensity": 400.0},
        {"from": "2026-01-01T01:00Z", "to": "2026-01-01T02:00Z",
         "intensity": 200.0},
    ]
    path = tmp_path / "series.json"
    path.write_text(json.dumps(series))
    points = ci.FileProvider(path=path, unit="gco2e_per_kwh").series(
        zone="", start=0.0, end=99_999_999_999.0, basis="location")
    assert points[0].kgco2e_per_kwh == pytest.approx(0.4)
    assert points[1].kgco2e_per_kwh == pytest.approx(0.2)


def test_file_provider_csv_kg(tmp_path):
    path = tmp_path / "series.csv"
    path.write_text("timestamp,intensity\n"
                    "2026-01-01T00:00:00Z,0.3\n"
                    "2026-01-01T01:00:00Z,0.5\n")
    points = ci.FileProvider(path=path, unit="kgco2e_per_kwh").series(
        zone="", start=0.0, end=99_999_999_999.0, basis="location")
    assert points[0].kgco2e_per_kwh == pytest.approx(0.3)
    # the end of each row is filled from the next row's start
    assert points[0].ts_end == pytest.approx(points[1].ts_start)


def test_file_provider_rejects_implausible_units(tmp_path):
    path = tmp_path / "series.csv"
    path.write_text("timestamp,intensity\n2026-01-01T00:00:00Z,400\n")
    # Declaring kg when the file is grams makes every value 1000x too large.
    with pytest.raises(ci.IntensityError) as excinfo:
        ci.FileProvider(path=path, unit="kgco2e_per_kwh").series(
            zone="", start=0.0, end=99_999_999_999.0, basis="location")
    assert "check the declared units" in str(excinfo.value)


def test_file_provider_window_filter(tmp_path):
    path = tmp_path / "series.json"
    path.write_text(json.dumps([
        {"from": "2026-01-01T00:00Z", "to": "2026-01-01T01:00Z",
         "intensity": 400.0}]))
    import datetime as dt

    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp()
    points = ci.FileProvider(path=path, unit="gco2e_per_kwh").series(
        zone="", start=start, end=start + 1800, basis="location")
    assert len(points) == 1


def test_missing_file_is_an_intensity_error_not_a_crash(tmp_path):
    with pytest.raises(ci.IntensityError):
        ci.FileProvider(path=tmp_path / "nope.json").series(
            zone="", start=0.0, end=1.0, basis="location")


def test_basis_mismatch_is_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "_http_get_json", lambda url, timeout: NESO_PAYLOAD)
    resolved = ci.resolve_intensity(
        carbon=_carbon(provider="neso-gb", basis="marginal"),
        accounting=_accounting(), start=0.0, end=3600.0, cache_dir=tmp_path)
    assert resolved.basis == "location"
    assert "basis_mismatch" in resolved.reason


def test_file_provider_without_a_path_explains_itself(tmp_path):
    resolved = ci.resolve_intensity(
        carbon=_carbon(provider="file", file=""), accounting=_accounting(),
        start=0.0, end=3600.0, cache_dir=tmp_path)
    assert resolved.points == ()
    assert "provider_file_not_configured" in resolved.reason


def test_configured_timeout_reaches_the_http_provider(tmp_path):
    """[carbon] timeout_s must not be an inert knob."""
    provider = ci.provider_for(_carbon(provider="neso-gb", timeout_s=2.5))
    assert provider.timeout == pytest.approx(2.5)

    default = ci.provider_for(_carbon(provider="neso-gb"))
    assert default.timeout == pytest.approx(5.0)

    # A nonsense value degrades to the default rather than to no timeout.
    assert ci.provider_for(
        _carbon(provider="neso-gb", timeout_s=0)).timeout == pytest.approx(5.0)
    assert ci.provider_for(
        _carbon(provider="neso-gb", timeout_s=-1)).timeout == pytest.approx(5.0)


def test_documented_provider_names_the_way_forward(tmp_path):
    resolved = ci.resolve_intensity(
        carbon=_carbon(provider="eia"), accounting=_accounting(),
        start=0.0, end=3600.0, cache_dir=tmp_path)
    assert "provider_not_implemented" in resolved.reason
    assert "docs/sci.md" in resolved.reason


def test_unknown_provider_is_distinguished_from_a_documented_one(tmp_path):
    resolved = ci.resolve_intensity(
        carbon=_carbon(provider="nonsense"), accounting=_accounting(),
        start=0.0, end=3600.0, cache_dir=tmp_path)
    assert "provider_unknown" in resolved.reason
    assert "provider_not_implemented" not in resolved.reason


def test_oversized_series_is_refused_not_read(tmp_path, monkeypatch):
    path = tmp_path / "big.csv"
    path.write_text("timestamp,intensity\n")
    monkeypatch.setattr(ci, "MAX_SERIES_BYTES", 4)
    with pytest.raises(ci.IntensityError) as excinfo:
        ci.FileProvider(path=path).series(zone="", start=0.0, end=1e12,
                                          basis="location")
    assert "series limit" in str(excinfo.value)


def test_non_finite_intensity_is_rejected(tmp_path):
    path = tmp_path / "series.csv"
    path.write_text("timestamp,intensity\n2026-01-01T00:00:00Z,NaN\n")
    with pytest.raises(ci.IntensityError) as excinfo:
        ci.FileProvider(path=path).series(zone="", start=0.0, end=1e12,
                                          basis="location")
    assert "finite" in str(excinfo.value)


def test_negative_intensity_is_rejected(tmp_path):
    path = tmp_path / "series.csv"
    path.write_text("timestamp,intensity\n2026-01-01T00:00:00Z,-50\n")
    with pytest.raises(ci.IntensityError) as excinfo:
        ci.FileProvider(path=path).series(zone="", start=0.0, end=1e12,
                                          basis="location")
    assert "outside" in str(excinfo.value)


def test_resolve_never_raises_on_a_hostile_file(tmp_path):
    """Whatever a configured file contains, resolve_intensity reports, not raises."""
    path = tmp_path / "binary.csv"
    path.write_bytes(b"\xff\xfe\x00\x01\x02not utf-8")
    resolved = ci.resolve_intensity(
        carbon=_carbon(provider="file", file=str(path)),
        accounting=_accounting(), start=0.0, end=9_999_999_999.0,
        cache_dir=tmp_path)
    assert resolved.points == ()
    assert resolved.reason.startswith("provider_error")


def test_neso_gb_url_is_widened_for_a_sub_minute_window(monkeypatch):
    import datetime as dt

    seen = {}

    def payload(url, timeout):
        seen["url"] = url
        return NESO_PAYLOAD

    monkeypatch.setattr(ci, "_http_get_json", payload)
    start = dt.datetime(2026, 1, 1, 0, 0, 30, tzinfo=dt.timezone.utc).timestamp()
    ci.resolve_intensity(carbon=_carbon(provider="neso-gb"),
                         accounting=_accounting(), start=start, end=start + 20,
                         cache_dir=None)
    start_iso, end_iso = seen["url"].rsplit("/", 2)[-2:]
    assert start_iso != end_iso, "from == to would return an empty range"


def test_zone_is_validated_before_it_reaches_the_url(monkeypatch):
    calls = []

    def payload(url, timeout):
        calls.append(url)
        return DK_PAYLOAD

    monkeypatch.setattr(ci, "_http_get_json", payload)
    with pytest.raises(ci.IntensityError):
        ci.EnergiDataServiceProvider().series(
            zone='DK1"]}&x=', start=0.0, end=600.0, basis="location")
    assert calls == []


def test_oversized_provider_response_is_refused(monkeypatch):
    """The HTTP body is bounded, like the series file."""
    class FakeResponse:
        def read(self, limit=None):
            return b"x" * (limit or 10)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(ci.urllib.request, "urlopen",
                        lambda request, timeout=None: FakeResponse())
    monkeypatch.setattr(ci, "MAX_RESPONSE_BYTES", 8)
    with pytest.raises(ci.IntensityError) as excinfo:
        ci._http_get_json("https://example.invalid/series", timeout=1.0)
    assert "exceeds" in str(excinfo.value)
