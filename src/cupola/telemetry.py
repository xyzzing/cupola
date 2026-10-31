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
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cupola.sensors import SensorReading, probe_sensors


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
    """Compute energy from cumulative energy counter delta (Tier 2)."""
    if first_energy_uj is None or last_energy_uj is None:
        return EnergyResult(
            energy_kwh=None, tier=2, source="none",
            sample_hz=sample_hz, missing_samples=0,
            method="energy_counter_delta", quality="unavailable"
        )

    delta_uj = last_energy_uj - first_energy_uj
    if delta_uj < 0:
        delta_uj = 0

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
        accounting: Optional[dict] = None,
        stop_callback=None,
    ):
        self.instance_uuid = instance_uuid
        self.profile = profile
        self.pid = pid
        self.start_time = start_time
        self.state_dir = state_dir
        self.sample_hz = sample_hz
        self.warn_hotspot_c = warn_hotspot_c
        self.critical_hotspot_c = critical_hotspot_c
        self.critical_hold_s = critical_hold_s
        self.auto_stop_on_critical = auto_stop_on_critical
        self.assume_power_w = assume_power_w
        self.wall_energy_path = wall_energy_path
        self.accounting = accounting or {}
        self.stop_callback = stop_callback

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._samples: list[SensorReading] = []
        self._first_energy_uj: Optional[int] = None
        self._last_energy_uj: Optional[int] = None
        self._session_start = time.time()
        self._max_hotspot_c: Optional[float] = None
        self._max_edge_c: Optional[float] = None
        self._power_sum = 0.0
        self._power_count = 0
        self._peak_power_w: Optional[float] = None
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
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._sample_loop,
            daemon=True,
            name=f"cupola-sentinel-{self.instance_uuid[:8]}",
        )
        self._thread.start()

    def stop(self) -> SessionSummary:
        """Stop sampling and return session summary."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        return self._write_summary()

    def _sample_loop(self) -> None:
        """Main sampling loop."""
        interval = 1.0 / self.sample_hz

        while not self._stop_event.is_set():
            try:
                reading = probe_sensors()
                self._samples.append(reading)

                # Track energy counters
                if reading.energy_uj is not None:
                    if self._first_energy_uj is None:
                        self._first_energy_uj = reading.energy_uj
                    self._last_energy_uj = reading.energy_uj

                # Track extrema
                if reading.gpu_hotspot_c is not None:
                    if self._max_hotspot_c is None or reading.gpu_hotspot_c > self._max_hotspot_c:
                        self._max_hotspot_c = reading.gpu_hotspot_c
                if reading.gpu_edge_c is not None:
                    if self._max_edge_c is None or reading.gpu_edge_c > self._max_edge_c:
                        self._max_edge_c = reading.gpu_edge_c

                # Track power statistics
                if reading.power_w is not None:
                    self._power_sum += reading.power_w
                    self._power_count += 1
                    if self._peak_power_w is None or reading.power_w > self._peak_power_w:
                        self._peak_power_w = reading.power_w

                # Update current state atomically
                self._update_current_state(reading)

                # Evaluate thermal policy
                self._evaluate_policy(reading)

            except Exception:
                import traceback
                traceback.print_exc()

            self._stop_event.wait(timeout=interval)

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
        energy = self._compute_energy(duration_s)

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

        avg_power = None
        if self._power_count > 0:
            avg_power = self._power_sum / self._power_count

        # Collect missing sensors across all samples
        missing_set = set()
        for sample in self._samples:
            for m in sample.missing:
                missing_set.add(m)

        summary = SessionSummary(
            instance_uuid=self.instance_uuid,
            profile=self.profile,
            start_time=datetime.fromtimestamp(self._session_start, tz=timezone.utc).isoformat(),
            end_time=datetime.now(timezone.utc).isoformat(),
            duration_s=duration_s,
            sample_count=len(self._samples),
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
            max_hotspot_c=self._max_hotspot_c,
            max_edge_c=self._max_edge_c,
            avg_power_w=avg_power,
            peak_power_w=self._peak_power_w,
            missing_sensors=list(missing_set),
            policy_events=self._policy_events,
        )

        # Write summary atomically
        update_state_atomic(summary.to_dict(), self._summary_path)

        return summary

    def _compute_energy(self, duration_s: float) -> EnergyResult:
        """Compute energy using tiered approach."""
        # Tier 2: energy counter delta
        if self._first_energy_uj is not None and self._last_energy_uj is not None:
            return compute_energy_tier2(
                self._first_energy_uj, self._last_energy_uj, self.sample_hz
            )

        # Tier 3: power integration
        if self._samples:
            return compute_energy_tier3(self._samples, self.sample_hz)

        # Tier 4: assumed power
        if self.assume_power_w is not None:
            return compute_energy_tier4(self.assume_power_w, duration_s)

        # Unavailable
        return EnergyResult(
            energy_kwh=None, tier=0, source="none",
            sample_hz=self.sample_hz, missing_samples=len(self._samples),
            method="none", quality="unavailable"
        )


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
