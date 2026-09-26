"""Software Carbon Intensity (SCI) — the carbon number, and its honesty.

Implements the base SCI specification (ISO/IEC 21031:2024) as used by the
Green Software Foundation's SCI-for-AI methodology::

    SCI = (E x I + M) / R

======================  ==================================================
``E``                   energy consumed by the software, in kWh
``I``                   carbon intensity of that energy, in kgCO2e/kWh
``M``                   embodied emissions of the hardware, in kgCO2e
``R``                   the functional unit — the useful work performed
======================  ==================================================

Two properties matter more than the arithmetic:

* **A missing number is never zero.** No energy, no intensity, or no
  functional unit yields ``sci = None`` and a machine-readable ``reason``.
  Reporting ``0`` would claim the workload was carbon-free.
* **Every number carries its provenance.** The energy tier, the intensity
  source/basis/resolution, and the embodied method travel with the result, so
  a reader can weigh an estimate for what it is. This module is pure: it does
  no I/O and knows nothing about sensors, HTTP, or configuration files.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

SCHEMA_VERSION = 1

#: SCI is conventionally presented per functional unit in grams.
GCO2E_PER_KGCO2E = 1000.0

#: Energy quality labels, derived from Sentinel's measurement tiers.
QUALITY_MEASURED = "measured"
QUALITY_ESTIMATED = "estimated"
QUALITY_ASSUMED = "assumed"
QUALITY_UNAVAILABLE = "unavailable"

#: Functional unit identifiers.
UNIT_VERIFIED_TASK = "verified_task"
UNIT_TOKENS_1K = "1k_tokens"
UNIT_SESSION_HOUR = "session_hour"

#: Evidence needs at least this many comparable runs (mirrors
#: ``cupola.compare.MIN_COMPARABLE_RUNS``; kept local to avoid a cycle).
MIN_RUNS_FOR_EVIDENCE = 5

#: Green Software Foundation SCI-for-AI fallback for the embodied term.
DEFAULT_EMBODIED_SHARE = 0.12
EMBODIED_SHARE_SOURCE = "GSF SCI-for-AI reference heuristic"
EMBODIED_SHARE_ASSUMPTION = (
    "M estimated as 12% of operational emissions; published studies put the "
    "embodied share at 8-15%. Supply embodied_total_kgco2e and "
    "embodied_lifetime_hours for an amortised figure instead."
)


@dataclass(frozen=True)
class CarbonIntensity:
    """The ``I`` term: where the electricity came from, and how well we know."""

    kgco2e_per_kwh: float
    source: str = "static"
    version: str = ""
    basis: str = "location"  # location | marginal
    region: str = ""
    resolution_s: int = 0  # 0 = annual/static
    matched: str = "flat"  # flat | hourly_weighted
    coverage: float = 1.0  # fraction of window energy with a resolved interval

    def to_dict(self) -> dict:
        return {
            "kgco2e_per_kwh": self.kgco2e_per_kwh,
            "source": self.source,
            "version": self.version,
            "basis": self.basis,
            "region": self.region,
            "resolution_s": self.resolution_s,
            "matched": self.matched,
            "coverage": self.coverage,
        }

    @classmethod
    def from_dict(cls, block: dict) -> "CarbonIntensity":
        """Rebuild from a persisted block, ignoring keys it does not own.

        Reports carry provenance as plain JSON; this is the one place that knows
        how to turn it back into a value, so no consumer repeats the field list
        (and they cannot drift apart).
        """
        return cls(
            kgco2e_per_kwh=float(block["kgco2e_per_kwh"]),
            source=str(block.get("source") or "static"),
            version=str(block.get("version") or ""),
            basis=str(block.get("basis") or "location"),
            region=str(block.get("region") or ""),
            resolution_s=int(block.get("resolution_s") or 0),
            matched=str(block.get("matched") or "flat"),
            coverage=float(block.get("coverage") or 0.0),
        )


@dataclass(frozen=True)
class EmbodiedEmissions:
    """The ``M`` term: the hardware's share of the work."""

    kgco2e: float
    method: str = "share_of_operational"
    source: str = EMBODIED_SHARE_SOURCE
    assumption: str = EMBODIED_SHARE_ASSUMPTION

    def to_dict(self) -> dict:
        return {
            "kgco2e": self.kgco2e,
            "method": self.method,
            "source": self.source,
            "assumption": self.assumption,
        }


@dataclass(frozen=True)
class FunctionalUnit:
    """The ``R`` term: what one unit of useful work means here."""

    unit: str
    count: float
    label: str = ""

    def to_dict(self) -> dict:
        return {"unit": self.unit, "count": self.count, "label": self.label}


@dataclass(frozen=True)
class SciResult:
    """One SCI computation, with everything needed to judge it."""

    sci: Optional[float]  # gCO2e per functional unit
    unit: str
    energy_kwh: Optional[float] = None
    energy_tier: int = 0
    energy_quality: str = QUALITY_UNAVAILABLE
    operational_gco2e: Optional[float] = None
    embodied_gco2e: Optional[float] = None
    total_gco2e: Optional[float] = None
    intensity: Optional[dict] = None
    embodied: Optional[dict] = None
    reason: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "sci": self.sci,
            "unit": self.unit,
            "energy_kwh": self.energy_kwh,
            "energy_tier": self.energy_tier,
            "energy_quality": self.energy_quality,
            "operational_gco2e": self.operational_gco2e,
            "embodied_gco2e": self.embodied_gco2e,
            "total_gco2e": self.total_gco2e,
            "intensity": self.intensity,
            "embodied": self.embodied,
            "reason": self.reason,
        }


def energy_quality_for_tier(tier: int) -> str:
    """Map a Sentinel measurement tier to an SCI energy-quality label."""
    if tier in (1, 2):
        return QUALITY_MEASURED
    if tier == 3:
        return QUALITY_ESTIMATED
    if tier == 4:
        return QUALITY_ASSUMED
    return QUALITY_UNAVAILABLE


def embodied_share_of_operational(
    operational_kgco2e: float,
    fraction: float = DEFAULT_EMBODIED_SHARE,
    source: str = EMBODIED_SHARE_SOURCE,
) -> EmbodiedEmissions:
    """Estimate ``M`` as a share of the operational term (GSF heuristic)."""
    return EmbodiedEmissions(
        kgco2e=max(0.0, operational_kgco2e * fraction),
        method="share_of_operational",
        source=source,
        assumption=EMBODIED_SHARE_ASSUMPTION,
    )


def embodied_amortised(
    total_kgco2e: float,
    lifetime_hours: float,
    workload_hours: float,
    source: str = "user-configured",
) -> EmbodiedEmissions:
    """Amortise a declared hardware embodied total across the workload."""
    if lifetime_hours <= 0:
        raise ValueError("lifetime_hours must be positive to amortise M")
    share = max(0.0, min(1.0, workload_hours / lifetime_hours))
    return EmbodiedEmissions(
        kgco2e=max(0.0, total_kgco2e) * share,
        method="amortised",
        source=source,
        assumption=(
            f"{workload_hours:.2f} h of a declared {lifetime_hours:.0f} h "
            f"service life"
        ),
    )


def effective_intensity(
    points: Sequence,
    samples: Iterable[tuple[float, Optional[float]]],
    fallback: Optional[float] = None,
) -> tuple[Optional[float], dict]:
    """Consumption-matched intensity over a measurement window.

    ``I_eff = sum(P_i * I(t_i) * dt_i) / sum(P_i * dt_i)``

    This is the "hourly matching" number: the intensity actually consumed,
    rather than the average of the hours the machine happened to be on. Grid
    intensity is published per interval, so the series is step-interpolated
    (a point covers ``[ts_start, ts_end)``), never linearly.

    ``samples`` is an iterable of ``(epoch_seconds, power_w)``; ``power_w`` may
    be ``None`` and is then excluded. With no usable points the ``fallback``
    (a static intensity) is returned and labelled ``flat``.

    Returns ``(intensity_kgco2e_per_kwh_or_None, detail)``. The detail carries
    ``matched``, ``coverage``, ``mean``, ``resolution_s`` and, on failure, a
    ``reason``.
    """
    usable = [(ts, p) for ts, p in samples if p is not None]
    if not usable:
        return fallback, {"matched": "flat" if fallback is not None else
                          "unavailable", "coverage": 0.0, "mean": None,
                          "resolution_s": 0, "reason": "no_power_samples"}

    usable.sort(key=lambda item: item[0])
    # dt_i is the slice each sample represents: the gap to the next sample,
    # and for the final sample the gap from the previous one.
    deltas = []
    for index, (ts, _power) in enumerate(usable):
        if index + 1 < len(usable):
            deltas.append(max(0.0, usable[index + 1][0] - ts))
        elif index > 0:
            deltas.append(max(0.0, ts - usable[index - 1][0]))
        else:
            deltas.append(1.0)

    # Index the series once: a linear scan per sample is quadratic in the run
    # length times the provider resolution (a 24 h session at 1 Hz against a
    # 5 min series is ~25M comparisons).
    ordered = sorted(points, key=lambda point: getattr(point, "ts_start", 0.0))
    starts = [getattr(point, "ts_start", 0.0) for point in ordered]

    weighted_carbon = 0.0
    covered_energy = 0.0
    total_energy = 0.0
    time_weighted: list[tuple[float, float]] = []

    for (ts, power), dt in zip(usable, deltas):
        energy = max(0.0, float(power)) * dt  # J-scale; units cancel in the ratio
        total_energy += energy
        point = _point_at(ordered, starts, ts)
        if point is None:
            continue
        covered_energy += energy
        weighted_carbon += energy * point.kgco2e_per_kwh
        time_weighted.append((point.kgco2e_per_kwh, dt))

    resolutions = [p.resolution_s for p in points if getattr(p, "resolution_s", 0)]
    resolution_s = min(resolutions) if resolutions else 0

    if total_energy <= 0.0:
        return fallback, {"matched": "flat" if fallback is not None else
                          "unavailable", "coverage": 0.0, "mean": None,
                          "resolution_s": resolution_s,
                          "reason": "zero_energy"}

    if covered_energy <= 0.0:
        return fallback, {"matched": "flat" if fallback is not None else
                          "unavailable", "coverage": 0.0, "mean": None,
                          "resolution_s": resolution_s,
                          "reason": "no_covered_energy"}

    # The intensity of the energy that could be matched. The uncovered share
    # is disclosed through ``coverage`` rather than quietly assumed away.
    return weighted_carbon / covered_energy, {
        "matched": "hourly_weighted",
        "coverage": round(min(1.0, covered_energy / total_energy), 4),
        "mean": _time_mean(time_weighted),
        "resolution_s": resolution_s,
    }


def _point_at(points: Sequence, starts: Sequence[float], ts: float):
    """The published interval covering ``ts``, looked up in O(log n).

    ``points`` must be ordered by ``ts_start``; ``starts`` is that ordering,
    precomputed. The interval is half-open (``[start, end)``) because grid
    intensity is published per interval, not per instant. One interval is
    assumed per timestamp: ``starts`` is sorted, so the last interval starting
    at or before ``ts`` wins.
    """
    index = bisect.bisect_right(starts, ts) - 1
    if index < 0:
        return None
    point = points[index]
    end = getattr(point, "ts_end", None)
    if end is None or ts >= end:
        return None
    return point


def _time_mean(pairs: Sequence[tuple[float, float]]) -> Optional[float]:
    total_dt = sum(dt for _value, dt in pairs)
    if total_dt <= 0:
        return None
    return sum(value * dt for value, dt in pairs) / total_dt


def compute_sci(
    *,
    energy_kwh: Optional[float],
    energy_tier: int = 0,
    intensity: Optional[CarbonIntensity] = None,
    embodied: Optional[EmbodiedEmissions] = None,
    functional_unit: Optional[FunctionalUnit] = None,
    allow_assumed_power: bool = False,
    energy_quality: Optional[str] = None,
) -> SciResult:
    """Compute SCI, or explain precisely why it cannot be computed.

    ``energy_quality`` overrides the tier-derived label when a caller knows
    better (for example a tier-3 figure the operator declared measured).
    """
    quality = energy_quality or energy_quality_for_tier(energy_tier)
    unit = functional_unit.unit if functional_unit else ""
    intensity_block = intensity.to_dict() if intensity else None
    embodied_block = embodied.to_dict() if embodied else None

    def blocked(reason: str, **overrides) -> SciResult:
        base = dict(
            sci=None,
            unit=unit,
            energy_tier=energy_tier,
            energy_quality=quality,
            intensity=intensity_block,
            embodied=embodied_block,
            reason=reason,
        )
        base.update(overrides)
        return SciResult(**base)

    if energy_kwh is None or energy_tier == 0 or quality == QUALITY_UNAVAILABLE:
        return blocked("energy_unavailable", energy_kwh=energy_kwh)

    if energy_tier == 4 and not allow_assumed_power:
        return blocked("assumed_power_not_allowed", energy_kwh=energy_kwh)

    if intensity is None:
        return blocked("intensity_unconfigured", energy_kwh=energy_kwh)

    operational_kg = energy_kwh * intensity.kgco2e_per_kwh
    if embodied is None:
        # Transparent zero: recorded as such rather than silently omitted.
        embodied = EmbodiedEmissions(
            kgco2e=0.0,
            method="none",
            source="unspecified",
            assumption="M not supplied; reported as 0 (embodied term unproven)",
        )
        embodied_block = embodied.to_dict()

    operational_g = operational_kg * GCO2E_PER_KGCO2E
    embodied_g = embodied.kgco2e * GCO2E_PER_KGCO2E
    total_g = operational_g + embodied_g

    if functional_unit is None or functional_unit.count <= 0:
        return SciResult(
            sci=None,
            unit=unit,
            energy_kwh=round(energy_kwh, 6),
            energy_tier=energy_tier,
            energy_quality=quality,
            operational_gco2e=round(operational_g, 6),
            embodied_gco2e=round(embodied_g, 6),
            total_gco2e=round(total_g, 6),
            intensity=intensity_block,
            embodied=embodied_block,
            reason="functional_unit_unavailable",
        )

    return SciResult(
        sci=round(total_g / functional_unit.count, 4),
        unit=functional_unit.unit,
        energy_kwh=round(energy_kwh, 6),
        energy_tier=energy_tier,
        energy_quality=quality,
        operational_gco2e=round(operational_g, 6),
        embodied_gco2e=round(embodied_g, 6),
        total_gco2e=round(total_g, 6),
        intensity=intensity_block,
        embodied=embodied_block,
        reason="",
    )


# --- green-software principles --------------------------------------------

PRINCIPLE_ENERGY_MEASURED = "energy_measured"
PRINCIPLE_INTENSITY_DECLARED = "intensity_declared"
PRINCIPLE_HOURLY_MATCHED = "hourly_matched"
PRINCIPLE_EMBODIED_ACCOUNTED = "embodied_accounted"
PRINCIPLE_UNIT_DECLARED = "unit_declared"
PRINCIPLE_CARBON_EFFICIENCY_PROVEN = "carbon_efficiency_proven"
PRINCIPLE_CARBON_AWARENESS = "carbon_awareness"

PRINCIPLE_ORDER = (
    PRINCIPLE_ENERGY_MEASURED,
    PRINCIPLE_INTENSITY_DECLARED,
    PRINCIPLE_HOURLY_MATCHED,
    PRINCIPLE_EMBODIED_ACCOUNTED,
    PRINCIPLE_UNIT_DECLARED,
    PRINCIPLE_CARBON_EFFICIENCY_PROVEN,
    PRINCIPLE_CARBON_AWARENESS,
)

PRINCIPLE_TITLES = {
    PRINCIPLE_ENERGY_MEASURED: "energy efficiency (energy is measured, not assumed)",
    PRINCIPLE_INTENSITY_DECLARED: "carbon intensity is declared with its provenance",
    PRINCIPLE_HOURLY_MATCHED: "hourly matching (intensity resolved to the hour)",
    PRINCIPLE_EMBODIED_ACCOUNTED: "hardware efficiency (embodied emissions accounted)",
    PRINCIPLE_UNIT_DECLARED: "comparability (functional unit is declared)",
    PRINCIPLE_CARBON_EFFICIENCY_PROVEN: "carbon efficiency (proven against a baseline)",
    PRINCIPLE_CARBON_AWARENESS: "carbon awareness (marginal intensity is used)",
}

#: Only these two statuses exist. ``pass`` requires positive evidence;
#: everything else is ``unproven`` with its reason. There is no ``fail`` yet
#: because cupola has no registry of carbon claims to contradict.
STATUS_PASS = "pass"
STATUS_UNPROVEN = "unproven"


def _principle(principle_id: str, status: str, evidence: str,
               limitation: str = "") -> dict:
    return {
        "id": principle_id,
        "principle": PRINCIPLE_TITLES[principle_id],
        "status": status,
        "evidence": evidence,
        "limitation": limitation,
    }


def carbon_principles(report: dict) -> list[dict]:
    """Evaluate green-software principles from a report's own evidence.

    Unknown never becomes green: a principle that cannot be evidenced is
    ``unproven``, with the reason stated. See ``docs/sci.md``.
    """
    metrics = report.get("metrics") or {}
    carbon = metrics.get("carbon") or {}
    intensity = carbon.get("intensity") or {}
    embodied = carbon.get("embodied") or {}
    principles: list[dict] = []

    tiers = carbon.get("tiers") or {}
    measured_runs = int(tiers.get("1", 0)) + int(tiers.get("2", 0))
    assumed_runs = int(tiers.get("4", 0))
    if assumed_runs:
        principles.append(_principle(
            PRINCIPLE_ENERGY_MEASURED, STATUS_UNPROVEN,
            f"{assumed_runs} run(s) used electrically assumed power",
            "assumed power is a guess, not a measurement",
        ))
    elif measured_runs >= MIN_RUNS_FOR_EVIDENCE:
        principles.append(_principle(
            PRINCIPLE_ENERGY_MEASURED, STATUS_PASS,
            f"{measured_runs} run(s) at a metered energy tier (1-2)"))
    else:
        principles.append(_principle(
            PRINCIPLE_ENERGY_MEASURED, STATUS_UNPROVEN,
            f"{measured_runs} metered run(s); {MIN_RUNS_FOR_EVIDENCE} needed",
            "energy came from power integration or was unavailable",
        ))

    if intensity.get("source") and intensity.get("version") and \
            intensity.get("basis"):
        principles.append(_principle(
            PRINCIPLE_INTENSITY_DECLARED, STATUS_PASS,
            f"I = {intensity.get('kgco2e_per_kwh')} kgCO2e/kWh from "
            f"{intensity.get('source')} ({intensity.get('basis')}, "
            f"version {intensity.get('version')})"))
    else:
        principles.append(_principle(
            PRINCIPLE_INTENSITY_DECLARED, STATUS_UNPROVEN,
            "intensity source, version or basis is missing",
            "an undeclared factor cannot be checked or refreshed",
        ))

    resolution = int(intensity.get("resolution_s") or 0)
    coverage = float(intensity.get("coverage") or 0.0)
    if intensity.get("matched") == "hourly_weighted" and \
            0 < resolution <= 3600 and coverage >= 0.95:
        principles.append(_principle(
            PRINCIPLE_HOURLY_MATCHED, STATUS_PASS,
            f"I_eff consumption-matched at {resolution}s resolution "
            f"({coverage:.0%} of the window covered)"))
    else:
        principles.append(_principle(
            PRINCIPLE_HOURLY_MATCHED, STATUS_UNPROVEN,
            f"matched={intensity.get('matched') or 'flat'}, "
            f"resolution={resolution}s, coverage={coverage:.0%}",
            "a flat or annual factor cannot evidence hourly matching",
        ))

    if embodied.get("method") in ("amortised", "provider"):
        principles.append(_principle(
            PRINCIPLE_EMBODIED_ACCOUNTED, STATUS_PASS,
            f"M = {embodied.get('kgco2e')} kgCO2e ({embodied.get('method')})"))
    else:
        principles.append(_principle(
            PRINCIPLE_EMBODIED_ACCOUNTED, STATUS_UNPROVEN,
            f"M method: {embodied.get('method') or 'none'}",
            "the embodied term is heuristic or absent",
        ))

    sci = metrics.get("sci_gco2e_per_verified_task")
    if sci is not None and carbon.get("unit"):
        principles.append(_principle(
            PRINCIPLE_UNIT_DECLARED, STATUS_PASS,
            f"SCI = {sci} gCO2e per {carbon.get('unit')}"))
    else:
        principles.append(_principle(
            PRINCIPLE_UNIT_DECLARED, STATUS_UNPROVEN,
            "no SCI value with a declared functional unit"))

    comparison = carbon.get("comparison") or {}
    delta = comparison.get("sci_delta_gco2e_per_verified_task")
    if comparison.get("comparable") and delta is not None and delta <= 0 and \
            not comparison.get("completion_dropped"):
        principles.append(_principle(
            PRINCIPLE_CARBON_EFFICIENCY_PROVEN, STATUS_PASS,
            f"SCI delta {delta} gCO2e per verified task "
            f"({comparison.get('label', 'candidate vs baseline')})"))
    else:
        principles.append(_principle(
            PRINCIPLE_CARBON_EFFICIENCY_PROVEN, STATUS_UNPROVEN,
            "no comparable baseline/candidate SCI pair in this report",
            "carbon efficiency needs an A/B with matching methodology",
        ))

    if intensity.get("basis") == "marginal":
        principles.append(_principle(
            PRINCIPLE_CARBON_AWARENESS, STATUS_PASS,
            "I is a marginal signal, which is what changes when load shifts"))
    else:
        principles.append(_principle(
            PRINCIPLE_CARBON_AWARENESS, STATUS_UNPROVEN,
            f"I basis is {intensity.get('basis') or 'undeclared'}",
            "location-based averages cannot evidence demand shifting",
        ))

    return principles


@dataclass
class CarbonBlock:
    """The per-report carbon summary stored under ``metrics['carbon']``."""

    schema_version: int = SCHEMA_VERSION
    unit: str = ""
    intensity: Optional[dict] = None
    embodied: Optional[dict] = None
    energy_quality: str = QUALITY_UNAVAILABLE
    tiers: dict = field(default_factory=dict)
    principles: list[dict] = field(default_factory=list)
    comparison: dict = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "unit": self.unit,
            "intensity": self.intensity,
            "embodied": self.embodied,
            "energy_quality": self.energy_quality,
            "tiers": dict(self.tiers),
            "principles": list(self.principles),
            "comparison": dict(self.comparison),
            "reason": self.reason,
        }


def format_sci_lines(payload: dict) -> list[str]:
    """Human lines for a carbon payload, in either shape it reaches us.

    Accepts a report's ``metrics`` object or a Sentinel session summary, so
    ``cupola sci``, ``cupola telemetry --session`` and the report writer all
    present the same number the same way.
    """
    if not isinstance(payload, dict):
        return ["SCI: unavailable"]
    sci_block = payload.get("sci") if isinstance(payload.get("sci"), dict) \
        else None
    carbon = payload.get("carbon") if isinstance(payload.get("carbon"), dict) \
        else None

    if sci_block is None and carbon is None:
        reason = payload.get("sci_reason") or payload.get("reason")
        return [f"SCI: unavailable ({reason})" if reason
                else "SCI: unavailable"]

    if sci_block is not None:
        value = sci_block.get("sci")
        if value is None:
            return [f"SCI: unavailable ({sci_block.get('reason') or 'unknown'})"]
        lines = [
            f"SCI: {value} gCO2e per {sci_block.get('unit')} "
            f"[{sci_block.get('energy_quality')}]",
            f"Operational: {sci_block.get('operational_gco2e')} gCO2e | "
            f"Embodied: {sci_block.get('embodied_gco2e')} gCO2e",
        ]
        intensity = sci_block.get("intensity")
        if isinstance(intensity, dict):
            lines.append(
                f"I: {intensity.get('kgco2e_per_kwh')} kgCO2e/kWh "
                f"({intensity.get('source')}, {intensity.get('basis')}, "
                f"matched={intensity.get('matched')})")
        return lines

    value = payload.get("sci_gco2e_per_verified_task")
    if value is None:
        return [f"SCI: unavailable ({carbon.get('reason') or 'unknown'})"]
    lines = [
        f"SCI: {value} gCO2e per {carbon.get('unit') or 'functional unit'} "
        f"[{carbon.get('energy_quality')}]",
    ]
    per_success = payload.get("sci_gco2e_per_success")
    if per_success is not None and per_success != value:
        lines.append(f"Per success (all spend / wins): {per_success} gCO2e")
    per_tokens = payload.get("sci_gco2e_per_1k_tokens")
    if per_tokens is not None:
        lines.append(f"Per 1k tokens: {per_tokens} gCO2e")
    lines.append(f"Energy: {payload.get('energy_kwh_total')} kWh total over "
                 f"{payload.get('energy_runs')} run(s)")
    intensity = carbon.get("intensity")
    if isinstance(intensity, dict):
        lines.append(
            f"I: {intensity.get('kgco2e_per_kwh')} kgCO2e/kWh "
            f"({intensity.get('source')}, {intensity.get('basis')}, "
            f"matched={intensity.get('matched')})")
    embodied = carbon.get("embodied")
    if isinstance(embodied, dict):
        lines.append(f"M: {embodied.get('method')}")
    return lines
