"""Sentinel — session-scoped telemetry, energy, and thermal policy.

Polls hardware sensors at configurable cadence during a Cupola-owned session,
computes VRAM headroom, thermal state, and energy consumption. Writes to
instance.json atomically via temp file + rename.

Measurement tiers:
  1: Wall-meter file/counter (user-configured)
  2: GPU hwmon energy1_input (µJ)
  3: GPU hwmon power1_average integration
  4: Explicit assume_power_w (user-configured estimate)
  unavailable: No power/energy data

Energy here is the ``E`` term of Software Carbon Intensity. The session
summary therefore carries an ``sci`` block (see :mod:`cupola.sci`) alongside
the older operational-only ``co2e_kg`` estimate.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from cupola import carbon_intensity as ci
from cupola import sci as sci_mod
from cupola.sci import CarbonIntensity, FunctionalUnit
from cupola.sensors import SensorReading, probe_sensors

#: Shortest session that can carry a per-hour rate. Below this the window is
#: smaller than the measurement it would be normalised by.
MIN_SESSION_SECONDS = 1.0

#: Wall-meter counter units -> kWh. Declared in config, never inferred.
_WALL_UNIT_TO_KWH = {
    "kwh": 1.0,
    "wh": 0.001,
    "j": 1.0 / 3_600_000.0,
}


def _read_wall_counter(path: Optional[str],
                       unit: str = "kwh") -> Optional[float]:
    """Read a wall-meter counter file and convert it to kWh.

    A missing file, an unreadable value, or an undeclared unit returns
    ``None`` rather than a guess: a wrong wall reading is worse than no wall
    reading, because it silently upgrades the quality tier.
    """
    if not path:
        return None
    factor = _WALL_UNIT_TO_KWH.get((unit or "kwh").lower())
    if factor is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read().strip()
        return float(raw) * factor
    except (OSError, ValueError):
        return None


def _sample_pairs(samples: list[SensorReading]) -> list[tuple[float, Optional[float]]]:
    """``(epoch_seconds, power_w)`` pairs, for consumption-matched intensity."""
    pairs = []
    for sample in samples:
        stamp = sample.ts
        try:
            parsed = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        pairs.append((parsed.timestamp(), sample.power_w))
    return pairs


def build_intensity(
    *,
    carbon,
    accounting,
    start: float,
    end: float,
    samples: list[SensorReading],
    cache_dir: Optional[Path] = None,
) -> tuple[Optional[CarbonIntensity], dict]:
    """Resolve ``I`` for a window: provider series, else the static factor.

    Returns ``(CarbonIntensity | None, detail)``. The detail carries the
    resolution decision (``reason``, ``series_sha256``, coverage) so a report
    can record why a number is coarse instead of hiding it.

    Two rules this layer must not break:

    * ``require_hourly`` is a promise made to the operator, so a refusal from
      :func:`resolve_intensity` is honoured here too — the static factor is
      *not* substituted for a refused hourly lookup, or the setting would be
      defeated one layer above the code that implements it.
    * a value that came from the static fallback is labelled as static, even
      when a provider series was resolved. Stamping the provider's identity on
      the accounting factor would misattribute the number and make two
      different methodologies look identical to :mod:`cupola.compare`.
    """
    resolved = ci.resolve_intensity(
        carbon=carbon, accounting=accounting, start=start, end=end,
        cache_dir=cache_dir)
    static_value = getattr(accounting, "emissions_factor_kgco2e_per_kwh", None)
    detail = {
        # ``source`` is who produced the number; ``requested_provider`` is what
        # was asked for. Conflating them is how a static fallback ends up
        # wearing a provider's name.
        "source": resolved.source,
        "requested_provider": str(getattr(carbon, "provider", "static")),
        "resolution_s": resolved.resolution_s,
        "basis": resolved.basis,
        "region": resolved.region,
        "series_sha256": resolved.series_sha256,
        "reason": resolved.reason,
    }

    def static_intensity() -> Optional[CarbonIntensity]:
        """The configured annual factor, labelled as exactly that."""
        if static_value is None:
            return None
        detail.update({
            "source": str(getattr(accounting, "emissions_factor_source", "")
                          or "static"),
            "resolution_s": 0,
            "basis": str(getattr(accounting, "emissions_intensity_basis",
                                  "location")),
            "matched": "flat",
            "coverage": 0.0,
            "mean": None,
            "used_fallback": True,
            "series_sha256": "",
        })
        return CarbonIntensity(
            kgco2e_per_kwh=float(static_value),
            source=str(getattr(accounting, "emissions_factor_source", "")
                       or "static"),
            version=str(getattr(accounting, "emissions_factor_version", "")
                        or "static"),
            basis=str(getattr(accounting, "emissions_intensity_basis",
                              "location")),
            region=str(getattr(accounting, "emissions_region", "") or ""),
            resolution_s=0,
            matched="flat",
            coverage=0.0,
        )

    if resolved.points:
        value, matched = sci_mod.effective_intensity(
            resolved.points, _sample_pairs(samples), fallback=None)
        detail.update(matched)
        if value is None:
            # The series could not be matched to this window's energy. With
            # require_hourly the operator asked for an hourly figure or none.
            if not getattr(carbon, "require_hourly", False):
                return static_intensity(), detail
            detail.setdefault(
                "reason", matched.get("reason") or "intensity_unresolved")
            return None, detail
        return CarbonIntensity(
            kgco2e_per_kwh=value,
            source=resolved.source,
            version=resolved.version or resolved.source,
            basis=resolved.basis,
            region=resolved.region,
            resolution_s=resolved.resolution_s,
            matched=detail.get("matched", "flat"),
            coverage=float(detail.get("coverage") or 0.0),
        ), detail

    if getattr(carbon, "require_hourly", False):
        detail.setdefault("reason", "intensity_hourly_unavailable")
        return None, detail
    if static_value is None:
        detail.setdefault("reason", "intensity_unconfigured")
        return None, detail
    return static_intensity(), detail


def compute_embodied(*, accounting, operational_kgco2e: float,
                     workload_hours: float):
    """The ``M`` term, by the method declared in ``[accounting]``.

    ``embodied_method = "amortised"`` needs a positive
    ``embodied_total_kgco2e`` and ``embodied_lifetime_hours``. When it cannot
    be honoured the heuristic is used, and the substitution is written into the
    result's ``assumption`` rather than silently changing the method: a config
    that is quietly ignored is worse than one that is rejected.
    """
    method = str(getattr(accounting, "embodied_method", "share_of_operational"))
    if method == "amortised":
        total = float(getattr(accounting, "embodied_total_kgco2e", 0.0) or 0.0)
        lifetime = float(getattr(accounting, "embodied_lifetime_hours", 0.0) or 0.0)
        if total > 0.0 and lifetime > 0.0:
            return sci_mod.embodied_amortised(
                total, lifetime, workload_hours,
                source=str(getattr(accounting, "emissions_factor_source", "")
                           or "user-configured"))
        heuristic = sci_mod.embodied_share_of_operational(
            operational_kgco2e,
            fraction=float(getattr(accounting, "embodied_share_of_operational",
                                   0.12) or 0.12),
        )
        return replace(heuristic, assumption=(
            "embodied_method = 'amortised' was configured but "
            "embodied_total_kgco2e and embodied_lifetime_hours are both "
            "required to be positive; using the heuristic instead. "
            + heuristic.assumption))
    return sci_mod.embodied_share_of_operational(
        operational_kgco2e,
        fraction=float(getattr(accounting, "embodied_share_of_operational",
                               0.12) or 0.12),
    )


@dataclass
class TelemetryState:
    """Current telemetry state with schema versioning."""

    schema_version: int = 2
    vram_available_mb: Optional[int] = None
    vram_headroom_mb: Optional[int] = None
    hotspot_celsius: Optional[float] = None
    edge_celsius: Optional[float] = None
    power_w: Optional[float] = None
    quality: str = "unavailable"
    pacing_active: bool = False
    gtt_spill_detected: bool = False
    missing: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "vram_available_mb": self.vram_available_mb,
            "vram_headroom_mb": self.vram_headroom_mb,
            "hotspot_celsius": self.hotspot_celsius,
            "edge_celsius": self.edge_celsius,
            "power_w": self.power_w,
            "quality": self.quality,
            "pacing_active": self.pacing_active,
            "gtt_spill_detected": self.gtt_spill_detected,
            "missing": self.missing,
            "timestamp": self.timestamp,
        }


def update_state_atomic(state_data: dict, target_path: Path) -> None:
    """Atomically update state file via temp file + rename."""
    temp_path = target_path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(state_data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temp_path.replace(target_path)


@dataclass
class EnergyResult:
    """Energy computation result with tier information."""

    energy_kwh: Optional[float]
    tier: int  # 1-4, or 0 for unavailable
    source: str
    sample_hz: float
    missing_samples: int
    method: str
    quality: str

    def to_dict(self) -> dict:
        return {
            "energy_kwh": self.energy_kwh,
            "tier": self.tier,
            "source": self.source,
            "sample_hz": self.sample_hz,
            "missing_samples": self.missing_samples,
            "method": self.method,
            "quality": self.quality,
        }


def compute_energy_tier3(samples: list[SensorReading], sample_hz: float) -> EnergyResult:
    """Compute energy via rectangular integration of power samples (Tier 3).

    E_kWh = sum(P_i * dt_i) / 3,600,000

    When no sample carries a power value the result is *unavailable*, not zero:
    an integration with nothing to integrate is absence, and reporting 0 kWh
    would claim the hardware consumed no energy.
    """
    if not samples:
        return EnergyResult(
            energy_kwh=None, tier=3, source="none",
            sample_hz=sample_hz, missing_samples=0,
            method="rectangular_integration", quality="unavailable"
        )

    total_joules = 0.0
    missing = 0
    dt = 1.0 / sample_hz

    for sample in samples:
        if sample.power_w is not None:
            total_joules += sample.power_w * dt
        else:
            missing += 1

    if missing == len(samples):
        return EnergyResult(
            energy_kwh=None, tier=3, source="sysfs:power1_average",
            sample_hz=sample_hz, missing_samples=missing,
            method="rectangular_integration", quality="unavailable"
        )

    energy_kwh = total_joules / 3600000.0

    quality = "ok" if missing == 0 else "degraded"
    return EnergyResult(
        energy_kwh=energy_kwh, tier=3,
        source="sysfs:power1_average",
        sample_hz=sample_hz, missing_samples=missing,
        method="rectangular_integration", quality=quality
    )


def compute_energy_tier2(
    first_energy_uj: Optional[int],
    last_energy_uj: Optional[int],
    sample_hz: float
) -> EnergyResult:
    """Compute energy from cumulative energy counter delta (Tier 2).

    A counter that does not advance — or runs backwards across a driver reset —
    is a broken measurement, not a zero-energy session: the result is
    unavailable so nothing downstream can claim a measured zero.
    """
    if first_energy_uj is None or last_energy_uj is None:
        return EnergyResult(
            energy_kwh=None, tier=2, source="none",
            sample_hz=sample_hz, missing_samples=0,
            method="energy_counter_delta", quality="unavailable"
        )

    delta_uj = last_energy_uj - first_energy_uj
    if delta_uj <= 0:
        return EnergyResult(
            energy_kwh=None, tier=2, source="sysfs:energy1_input",
            sample_hz=sample_hz, missing_samples=0,
            method="energy_counter_delta", quality="unavailable"
        )

    energy_kwh = delta_uj / 3600000000000.0  # µJ to kWh

    return EnergyResult(
        energy_kwh=energy_kwh, tier=2,
        source="sysfs:energy1_input",
        sample_hz=sample_hz, missing_samples=0,
        method="energy_counter_delta", quality="ok"
    )


def compute_energy_tier4(assume_power_w: float, duration_s: float) -> EnergyResult:
    """Compute energy from assumed power (Tier 4, estimate only)."""
    energy_joules = assume_power_w * duration_s
    energy_kwh = energy_joules / 3600000.0

    return EnergyResult(
        energy_kwh=energy_kwh, tier=4,
        source="assume_power_w",
        sample_hz=0.0, missing_samples=0,
        method="assumed_power", quality="estimate"
    )


def compute_session_energy(
    *,
    samples: list[SensorReading],
    first_energy_uj: Optional[int] = None,
    last_energy_uj: Optional[int] = None,
    duration_s: float = 0.0,
    sample_hz: float = 1.0,
    assume_power_w: Optional[float] = None,
    wall_delta_kwh: Optional[float] = None,
) -> EnergyResult:
    """Pick the best available tier and compute energy.

    Tier 1 (wall meter) > tier 2 (energy counter) > tier 3 (power
    integration) > tier 4 (assumed power). A tier that cannot produce a number
    — a stalled counter, a run of missing power readings — falls through to the
    next one that can, and the result says which tier actually measured it.
    Shared by the session sampler and the benchmark :class:`EnergyMeter` so the
    two can never disagree.
    """
    if wall_delta_kwh is not None:
        return EnergyResult(
            energy_kwh=max(0.0, wall_delta_kwh), tier=1,
            source="wall_meter:file", sample_hz=0.0, missing_samples=0,
            method="counter_delta", quality="ok",
        )
    if first_energy_uj is not None and last_energy_uj is not None:
        counter = compute_energy_tier2(first_energy_uj, last_energy_uj, sample_hz)
        if counter.energy_kwh is not None:
            return counter
    if samples:
        integrated = compute_energy_tier3(samples, sample_hz)
        if integrated.energy_kwh is not None:
            return integrated
    if assume_power_w is not None:
        return compute_energy_tier4(assume_power_w, duration_s)
    return EnergyResult(
        energy_kwh=None, tier=0, source="none", sample_hz=sample_hz,
        missing_samples=0, method="none", quality="unavailable",
    )


class EnergyMeter:
    """Sample the hardware over a window, and turn it into tiered energy.

    One accumulator for both callers in this codebase: :class:`SentinelSampler`,
    which also runs thermal policy on every reading, and a benchmark run, which
    wants only the energy. It owns the sample list, the energy counters, the
    power statistics and the wall-meter readings, so the two cannot drift; what
    a caller does *with* a reading is a callback, not a second copy of this
    loop.
    """

    def __init__(
        self,
        *,
        sample_hz: float = 1.0,
        assume_power_w: Optional[float] = None,
        wall_energy_path: Optional[str] = None,
        wall_energy_unit: str = "kwh",
        probe=None,
        on_reading=None,
        on_error=None,
    ):
        self.sample_hz = max(0.01, float(sample_hz))
        self.assume_power_w = assume_power_w
        self.wall_energy_path = wall_energy_path
        self.wall_energy_unit = wall_energy_unit
        self._probe = probe or probe_sensors
        self._on_reading = on_reading
        self._on_error = on_error

        self._samples: list[SensorReading] = []
        self._first_energy_uj: Optional[int] = None
        self._last_energy_uj: Optional[int] = None
        self._power_sum = 0.0
        self._power_count = 0
        self._peak_power_w: Optional[float] = None
        self._wall_first: Optional[float] = None
        self._started_at = time.time()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- accumulation ------------------------------------------------------

    def sample(self) -> SensorReading:
        """Probe once and fold the reading into this meter."""
        reading = self._probe()
        self._samples.append(reading)
        if reading.energy_uj is not None:
            if self._first_energy_uj is None:
                self._first_energy_uj = reading.energy_uj
            self._last_energy_uj = reading.energy_uj
        if reading.power_w is not None:
            self._power_sum += reading.power_w
            self._power_count += 1
            if self._peak_power_w is None or reading.power_w > self._peak_power_w:
                self._peak_power_w = reading.power_w
        return reading

    @property
    def samples(self) -> list[SensorReading]:
        return list(self._samples)

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    @property
    def avg_power_w(self) -> Optional[float]:
        if self._power_count == 0:
            return None
        return self._power_sum / self._power_count

    @property
    def peak_power_w(self) -> Optional[float]:
        return self._peak_power_w

    @property
    def started_at(self) -> float:
        return self._started_at

    # -- a measured window -------------------------------------------------

    def start(self, thread_name: str = "cupola-energy") -> None:
        """Begin a measured window: clock, wall counter, sampling thread."""
        self._started_at = time.time()
        self._wall_first = _read_wall_counter(self.wall_energy_path,
                                              self.wall_energy_unit)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=thread_name)
        self._thread.start()

    def _loop(self) -> None:
        interval = 1.0 / self.sample_hz
        while not self._stop_event.is_set():
            try:
                reading = self.sample()
                if self._on_reading is not None:
                    self._on_reading(reading)
            except Exception as exc:
                if self._on_error is not None:
                    self._on_error(exc)
            self._stop_event.wait(timeout=interval)

    def halt(self) -> None:
        """Stop sampling. Idempotent, and safe before :meth:`start`."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def energy(self, duration_s: Optional[float] = None) -> EnergyResult:
        """Tiered energy for the window, reading the wall counter now.

        Call :meth:`halt` first for a settled figure; reading the counter while
        the loop still runs would extend the window under the caller's feet.
        """
        wall_last = _read_wall_counter(self.wall_energy_path,
                                       self.wall_energy_unit)
        wall_delta = None
        if self._wall_first is not None and wall_last is not None:
            wall_delta = max(0.0, wall_last - self._wall_first)
        if duration_s is None:
            duration_s = time.time() - self._started_at
        return compute_session_energy(
            samples=list(self._samples),
            first_energy_uj=self._first_energy_uj,
            last_energy_uj=self._last_energy_uj,
            duration_s=duration_s,
            sample_hz=self.sample_hz,
            assume_power_w=self.assume_power_w,
            wall_delta_kwh=wall_delta,
        )


@dataclass
class SessionSummary:
    """Durable session summary with energy and cost estimates."""

    instance_uuid: str
    profile: str
    start_time: str
    end_time: str
    duration_s: float
    sample_count: int
    energy: EnergyResult
    cost: Optional[float] = None
    currency: Optional[str] = None
    co2e_kg: Optional[float] = None
    emissions_factor: Optional[float] = None
    emissions_source: Optional[str] = None
    emissions_version: Optional[str] = None
    #: Software Carbon Intensity block (see cupola.sci). ``co2e_kg`` above is
    #: the operational term only; ``sci`` adds embodied emissions and divides
    #: by a functional unit.
    sci: Optional[dict] = None
    sci_reason: str = ""
    max_hotspot_c: Optional[float] = None
    max_edge_c: Optional[float] = None
    avg_power_w: Optional[float] = None
    peak_power_w: Optional[float] = None
    missing_sensors: list[str] = field(default_factory=list)
    policy_events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        result = {
            "instance_uuid": self.instance_uuid,
            "profile": self.profile,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_s": self.duration_s,
            "sample_count": self.sample_count,
            "energy": self.energy.to_dict(),
            "max_hotspot_c": self.max_hotspot_c,
            "max_edge_c": self.max_edge_c,
            "avg_power_w": self.avg_power_w,
            "peak_power_w": self.peak_power_w,
            "missing_sensors": self.missing_sensors,
            "policy_events": self.policy_events,
        }

        # Cost/CO2e only if computed
        if self.cost is not None:
            result["cost"] = self.cost
            result["currency"] = self.currency
        else:
            result["cost"] = None
            result["currency"] = None
            result["cost_reason"] = "accounting_unconfigured"

        if self.co2e_kg is not None:
            result["co2e_kg"] = self.co2e_kg
            result["emissions_factor"] = self.emissions_factor
            result["emissions_source"] = self.emissions_source
            result["emissions_version"] = self.emissions_version
        else:
            result["co2e_kg"] = None

        result["sci"] = dict(self.sci) if self.sci is not None else None
        if self.sci is None and self.sci_reason:
            result["sci_reason"] = self.sci_reason

        return result


class SentinelSampler:
    """Session-scoped telemetry sampler (Sentinel).

    Runs only after Crucible records a verified instance.
    Samples at configurable cadence, computes energy, applies thermal policy.
    """

    def __init__(
        self,
        instance_uuid: str,
        profile: str,
        pid: int,
        start_time: int,
        state_dir: Path,
        sample_hz: float = 1.0,
        warn_hotspot_c: float = 95.0,
        critical_hotspot_c: float = 105.0,
        critical_hold_s: int = 8,
        auto_stop_on_critical: bool = False,
        assume_power_w: Optional[float] = None,
        wall_energy_path: Optional[str] = None,
        wall_energy_unit: str = "kwh",
        accounting: Optional[dict] = None,
        carbon: Optional[dict] = None,
        cache_dir: Optional[Path] = None,
        probe=None,
        stop_callback=None,
    ):
        self.instance_uuid = instance_uuid
        self.profile = profile
        self.pid = pid
        self.start_time = start_time
        self.state_dir = state_dir
        self.warn_hotspot_c = warn_hotspot_c
        self.critical_hotspot_c = critical_hotspot_c
        self.critical_hold_s = critical_hold_s
        self.auto_stop_on_critical = auto_stop_on_critical
        self.accounting = accounting or {}
        self.carbon = carbon or {}
        self.cache_dir = cache_dir
        self.stop_callback = stop_callback

        # The one accumulator: it owns the samples, the energy counters, the
        # power statistics and the wall-meter readings. This class owns only
        # what is thermal or live (see _on_reading).
        self._meter = EnergyMeter(
            sample_hz=sample_hz,
            assume_power_w=assume_power_w,
            wall_energy_path=wall_energy_path,
            wall_energy_unit=wall_energy_unit,
            probe=probe,
            on_reading=self._on_reading,
            on_error=self._on_reading_error,
        )
        self._session_start = time.time()
        self._max_hotspot_c: Optional[float] = None
        self._max_edge_c: Optional[float] = None
        self._policy_events: list[dict] = []
        self._critical_since: Optional[float] = None

        # Paths
        self._current_path = state_dir / "telemetry" / "current.json"
        self._samples_path = state_dir / "telemetry" / "samples" / f"{instance_uuid}.jsonl"
        self._summary_path = state_dir / "telemetry" / "sessions" / f"{instance_uuid}.json"

        # Ensure directories
        self._current_path.parent.mkdir(parents=True, exist_ok=True)
        self._samples_path.parent.mkdir(parents=True, exist_ok=True)
        self._summary_path.parent.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        """Start the sampling thread."""
        self._meter.start(
            thread_name=f"cupola-sentinel-{self.instance_uuid[:8]}")

    def stop(self) -> SessionSummary:
        """Stop sampling and return session summary."""
        self._meter.halt()
        return self._write_summary()

    def _on_reading(self, reading: SensorReading) -> None:
        """Per-reading session work: thermal extrema, live state, policy."""
        if reading.gpu_hotspot_c is not None:
            if self._max_hotspot_c is None or \
                    reading.gpu_hotspot_c > self._max_hotspot_c:
                self._max_hotspot_c = reading.gpu_hotspot_c
        if reading.gpu_edge_c is not None:
            if self._max_edge_c is None or \
                    reading.gpu_edge_c > self._max_edge_c:
                self._max_edge_c = reading.gpu_edge_c
        self._update_current_state(reading)
        self._evaluate_policy(reading)

    def _on_reading_error(self, exc: Exception) -> None:
        """A probe or policy failure must not kill the sampling thread."""
        import traceback
        traceback.print_exception(exc)

    def _update_current_state(self, reading: SensorReading) -> None:
        """Write current telemetry state atomically."""
        # Compute VRAM headroom
        vram_available_mb = None
        vram_headroom_mb = None
        if reading.vram_used_bytes is not None and reading.vram_total_bytes is not None:
            available_bytes = reading.vram_total_bytes - reading.vram_used_bytes
            vram_available_mb = int(available_bytes / (1024 * 1024))
            vram_headroom_mb = vram_available_mb - 2560  # 2.5GB safety margin

        state = TelemetryState(
            vram_available_mb=vram_available_mb,
            vram_headroom_mb=vram_headroom_mb,
            hotspot_celsius=reading.gpu_hotspot_c,
            edge_celsius=reading.gpu_edge_c,
            power_w=reading.power_w,
            quality=reading.quality,
            missing=reading.missing,
        )

        state_data = {
            "instance_uuid": self.instance_uuid,
            "telemetry": state.to_dict(),
        }

        update_state_atomic(state_data, self._current_path)

    def _evaluate_policy(self, reading: SensorReading) -> None:
        """Evaluate thermal policy and trigger stop if needed."""
        temp = reading.gpu_hotspot_c
        if temp is None:
            temp = reading.gpu_edge_c

        if temp is None:
            # No temperature — degraded, no auto-stop
            self._critical_since = None
            return

        if temp >= self.critical_hotspot_c:
            if self._critical_since is None:
                self._critical_since = time.time()
                self._policy_events.append({
                    "type": "critical_started",
                    "temp_c": temp,
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
            elif time.time() - self._critical_since >= self.critical_hold_s:
                if self.auto_stop_on_critical and self.stop_callback:
                    self._policy_events.append({
                        "type": "stop_requested",
                        "temp_c": temp,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    })
                    self.stop_callback(self.instance_uuid)
        elif temp >= self.warn_hotspot_c:
            self._policy_events.append({
                "type": "warn",
                "temp_c": temp,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
        else:
            self._critical_since = None

    def _write_summary(self) -> SessionSummary:
        """Compute and write session summary."""
        duration_s = time.time() - self._session_start

        # Determine energy tier and compute
        energy = self._meter.energy(duration_s)

        # Compute cost/CO2e if accounting configured
        cost = None
        co2e_kg = None
        if self.accounting and energy.energy_kwh is not None:
            tariff = self.accounting.get("electricity_tariff_per_kwh")
            factor = self.accounting.get("emissions_factor_kgco2e_per_kwh")
            factor_source = self.accounting.get("emissions_factor_source")
            factor_version = self.accounting.get("emissions_factor_version")

            if tariff is not None:
                cost = energy.energy_kwh * tariff

            if factor is not None and factor_source and factor_version:
                if energy.tier <= 3 or (
                    energy.tier == 4
                    and self.accounting.get("allow_assumed_power")
                ):
                    co2e_kg = energy.energy_kwh * factor

        # Software Carbon Intensity: operational + embodied, per session hour.
        sci_block, sci_reason = self._compute_sci(energy, duration_s)

        # Collect missing sensors across all samples
        missing_set = set()
        for sample in self._meter.samples:
            for m in sample.missing:
                missing_set.add(m)

        summary = SessionSummary(
            instance_uuid=self.instance_uuid,
            profile=self.profile,
            start_time=datetime.fromtimestamp(self._session_start, tz=timezone.utc).isoformat(),
            end_time=datetime.now(timezone.utc).isoformat(),
            duration_s=duration_s,
            sample_count=self._meter.sample_count,
            energy=energy,
            cost=cost,
            currency=(
                self.accounting.get("currency") if self.accounting else None
            ),
            co2e_kg=co2e_kg,
            emissions_factor=(
                self.accounting.get("emissions_factor_kgco2e_per_kwh")
                if self.accounting
                else None
            ),
            emissions_source=(
                self.accounting.get("emissions_factor_source")
                if self.accounting
                else None
            ),
            emissions_version=(
                self.accounting.get("emissions_factor_version")
                if self.accounting
                else None
            ),
            sci=sci_block,
            sci_reason=sci_reason,
            max_hotspot_c=self._max_hotspot_c,
            max_edge_c=self._max_edge_c,
            avg_power_w=self._meter.avg_power_w,
            peak_power_w=self._meter.peak_power_w,
            missing_sensors=list(missing_set),
            policy_events=self._policy_events,
        )

        # Write summary atomically
        update_state_atomic(summary.to_dict(), self._summary_path)

        return summary

    def _compute_sci(self, energy: EnergyResult,
                     duration_s: float) -> tuple[Optional[dict], str]:
        """Build this session's SCI block (``(block, reason)``).

        A session's only defensible functional unit is the session hour: the
        sampler sees the hardware, not verified work, so it does not guess a
        per-task figure.
        """
        if not self.accounting:
            return None, "accounting_unconfigured"

        accounting = SimpleNamespace(**self.accounting)
        carbon = SimpleNamespace(**self.carbon) if self.carbon else \
            SimpleNamespace()

        energy_kwh = energy.energy_kwh
        if energy_kwh is None:
            return None, "energy_unavailable"

        end = time.time()
        intensity, detail = build_intensity(
            carbon=carbon,
            accounting=accounting,
            start=self._session_start,
            end=end,
            samples=self._meter.samples,
            cache_dir=self.cache_dir,
        )
        if intensity is None:
            return None, detail.get("reason") or "intensity_unavailable"

        hours = duration_s / 3600.0
        operational_kg = (
            energy_kwh * intensity.kgco2e_per_kwh if intensity is not None
            else 0.0
        )
        embodied = compute_embodied(
            accounting=accounting,
            operational_kgco2e=operational_kg,
            workload_hours=hours,
        )
        result = sci_mod.compute_sci(
            energy_kwh=energy_kwh,
            energy_tier=energy.tier,
            intensity=intensity,
            embodied=embodied,
            functional_unit=FunctionalUnit(
                unit=sci_mod.UNIT_SESSION_HOUR, count=hours,
                label="session hour"),
            allow_assumed_power=bool(
                self.accounting.get("allow_assumed_power")),
        )
        if result.sci is None:
            # Why the measurement itself was refused outranks why a rate could
            # not be formed from it.
            return None, result.reason or "sci_unavailable"
        if duration_s < MIN_SESSION_SECONDS:
            # A per-hour rate from a sub-second window is arithmetic, not a
            # measurement.
            return None, "session_too_short"
        block = result.to_dict()
        # The resolution decision travels with the number, so a summary says
        # whether it degraded and why.
        block["intensity_reason"] = detail.get("reason", "")
        block["used_fallback"] = bool(detail.get("used_fallback"))
        return block, ""


# Backward compatibility: old TelemetryBroadcaster name
TelemetryBroadcaster = SentinelSampler


def collect_telemetry() -> TelemetryState:
    """Collect current telemetry snapshot (backward compatible).

    Uses the new sensors probe and maps to TelemetryState format.
    """
    reading = probe_sensors()

    # Map to TelemetryState
    vram_available_mb = None
    vram_headroom_mb = None
    if reading.vram_used_bytes is not None and reading.vram_total_bytes is not None:
        available_bytes = reading.vram_total_bytes - reading.vram_used_bytes
        vram_available_mb = int(available_bytes / (1024 * 1024))
        vram_headroom_mb = vram_available_mb - 2560  # 2.5GB safety margin

    # Pacing: hotspot > 88°C or VRAM < 1.5GB
    pacing_active = False
    if reading.gpu_hotspot_c is not None and reading.gpu_hotspot_c > 88.0:
        pacing_active = True
    if vram_available_mb is not None and vram_available_mb < 1536:
        pacing_active = True

    return TelemetryState(
        vram_available_mb=vram_available_mb,
        vram_headroom_mb=vram_headroom_mb,
        hotspot_celsius=reading.gpu_hotspot_c,
        edge_celsius=reading.gpu_edge_c,
        power_w=reading.power_w,
        quality=reading.quality,
        pacing_active=pacing_active,
        gtt_spill_detected=reading.gtt_used_bytes is not None and reading.gtt_used_bytes > 0,
        missing=reading.missing,
    )
