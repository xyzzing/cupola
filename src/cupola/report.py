"""Benchmark reports: what was measured, with what, and what it cannot prove.

A report binds three things that must travel together or the numbers are
meaningless:

1. **the suite** — its fingerprint, so a changed task invalidates comparison;
2. **the systems** — what was under test, labelled by name;
3. **the lanes** — the toolchain that was actually installed at the time.

Reports also carry ``limitations``: every grader in the run states what it
cannot prove. A benchmark report that presents a pattern-match as a judgement
is worse than no report, because it will be believed.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from cupola.harness import System
from cupola.lanes import LaneSet
from cupola.runner import STS_BREACHED, STS_ERROR, STS_TIMEOUT, STS_VERIFIED
from cupola.sci import (
    UNIT_VERIFIED_TASK,
    CarbonBlock,
    CarbonIntensity,
    FunctionalUnit,
    carbon_principles,
    compute_sci,
    energy_quality_for_tier,
    format_sci_lines,
)
from cupola.suites import Suite
from cupola.telemetry import compute_embodied

REPORT_VERSION = 1

FAILED_STATUSES = (STS_TIMEOUT, STS_BREACHED, STS_ERROR)

#: Counters that must never be non-zero. A nonzero value fails a comparison
#: absolutely, at any sample size — the same rule minder's comparator uses.
SAFETY_COUNTERS = ("unsafe_executions", "prohibited_action_attempts",
                   "reproducibility_violations")

#: Carbon metrics, in the order they are presented. Flat on purpose: the
#: comparator walks numeric top-level keys, so a nested object would never
#: appear in a delta.
CARBON_METRIC_KEYS = (
    "energy_kwh_total",
    "operational_gco2e_total",
    "embodied_gco2e_total",
    "sci_gco2e_per_verified_task",
    "sci_gco2e_per_success",
    "sci_gco2e_per_1k_tokens",
    "energy_runs",
)


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


@dataclass
class Report:
    """One benchmark run's results."""

    suite_id: str
    suite_fingerprint: str
    systems: list[dict] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    environment: dict = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    report_version: int = REPORT_VERSION
    generated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "report_version": self.report_version,
            "generated_at": self.generated_at
            or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "suite_id": self.suite_id,
            "suite_fingerprint": self.suite_fingerprint,
            "systems": list(self.systems),
            "environment": dict(self.environment),
            "metrics": dict(self.metrics),
            "limitations": sorted(set(self.limitations)),
            "runs": list(self.runs),
        }

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return path


def build_report(suite: Suite, outcomes: Iterable, lane_set: Optional[LaneSet],
                 systems: Optional[Iterable[System]] = None,
                 lane_snapshot: Optional[Path] = None,
                 accounting: Optional[object] = None) -> Report:
    """Assemble a report from task outcomes plus the environment they ran in."""
    outcomes = list(outcomes)
    report = Report(
        suite_id=suite.suite_id,
        suite_fingerprint=suite.fingerprint(),
    )

    seen_systems: dict[str, dict] = {}
    if systems:
        for system in systems:
            seen_systems[system.name] = system.to_dict()
    for outcome in outcomes:
        seen_systems.setdefault(outcome.system, {"name": outcome.system})
    report.systems = [seen_systems[name] for name in sorted(seen_systems)]

    report.runs = [_run_row(outcome) for outcome in outcomes]
    report.metrics = derive_metrics(outcomes, accounting)
    report.limitations = _limitations(outcomes)

    environment: dict = {}
    if lane_set is not None:
        environment.update({
            "lane_set_hash": lane_set.lane_set_hash(),
            "lane_profile": lane_set.profile,
            "lane_count": len(lane_set.lanes),
            "lane_warnings": lane_set.warnings(),
        })
    if lane_snapshot is not None:
        environment["lane_snapshot"] = str(lane_snapshot)
    environment["reproducible"] = _reproducible(environment)
    report.environment = environment
    return report


def _run_row(outcome) -> dict:
    row = outcome.to_dict()
    row["usage"] = {
        "total_tokens": (outcome.usage or {}).get("total_tokens", 0),
        "input_tokens": (outcome.usage or {}).get("input_tokens", 0),
        "output_tokens": (outcome.usage or {}).get("output_tokens", 0),
        "cache_read_tokens": (outcome.usage or {}).get("cache_read_tokens", 0),
        "calls": (outcome.usage or {}).get("calls", 0),
    }
    row["verdicts"] = {
        grade.grader_type: grade.status for grade in outcome.grades
    }
    return row


def derive_metrics(outcomes: Iterable,
                   accounting: Optional[object] = None) -> dict:
    """Aggregate per-task outcomes into comparable metrics.

    Without ``accounting`` (no configured intensity and no tariff) the carbon
    block still exists, with ``sci`` absent and the reason recorded: an
    unmeasured machine must not look like a zero-carbon one.
    """
    outcomes = list(outcomes)
    total = len(outcomes)
    verified = sum(1 for o in outcomes if o.status == STS_VERIFIED)
    timed_out = sum(1 for o in outcomes if o.status == STS_TIMEOUT)
    breached = sum(1 for o in outcomes if o.status == STS_BREACHED)
    errored = sum(1 for o in outcomes if o.status == STS_ERROR)

    wall = [o.wall_seconds for o in outcomes if o.wall_seconds]
    tokens = [(o.usage or {}).get("total_tokens", 0) for o in outcomes]
    calls = [(o.usage or {}).get("calls", 0) for o in outcomes]
    output_tokens = [(o.usage or {}).get("output_tokens", 0)
                     for o in outcomes]

    # Two different questions, kept apart on purpose:
    #   *_per_verified_task  — what a successful task cost, on average
    #   *_per_success        — total spend over successes, i.e. the price of
    #                          one win once failures are paid for
    verified_wall = [o.wall_seconds for o in outcomes
                     if o.status == STS_VERIFIED and o.wall_seconds]
    verified_tokens = [(o.usage or {}).get("total_tokens", 0)
                       for o in outcomes if o.status == STS_VERIFIED]
    mean_wall = (round(sum(verified_wall) / len(verified_wall), 3)
                 if verified_wall else None)
    mean_tokens = (round(sum(verified_tokens) / len(verified_tokens), 1)
                   if verified_tokens else None)
    wall_per_success = round(sum(wall) / verified, 3) if verified else None
    tokens_per_success = round(sum(tokens) / verified, 1) if verified else None
    carbon_metrics, carbon_block = _derive_carbon(outcomes, accounting)
    metrics = {
        "comparable_runs": total,
        "verified": verified,
        "failed": total - verified - timed_out - breached - errored,
        "timed_out": timed_out,
        "breached": breached,
        "errored": errored,
        "verified_completion_rate": round(verified / total, 4) if total else 0.0,
        "wall_seconds_total": round(sum(wall), 3),
        "wall_seconds_p50": _percentile(wall, 50),
        "wall_seconds_p95": _percentile(wall, 95),
        "tokens_total": sum(tokens),
        "tokens_p50": _percentile(tokens, 50),
        "output_tokens_total": sum(output_tokens),
        "model_calls_total": sum(calls),
        "wall_seconds_per_verified_task": mean_wall,
        "tokens_per_verified_task": mean_tokens,
        "wall_seconds_per_success": wall_per_success,
        "tokens_per_success": tokens_per_success,
        "unsafe_executions": 0,
        "prohibited_action_attempts": 0,
        "reproducibility_violations": 0,
    }
    metrics.update(carbon_metrics)
    # The principles are evaluated against the finished metrics, so the block
    # is added last and then annotated in place.
    carbon_block.principles = carbon_principles(
        {"metrics": {**metrics, "carbon": carbon_block.to_dict()}})
    metrics["carbon"] = carbon_block.to_dict()
    return metrics


def _run_carbon(outcome, accounting) -> tuple[Optional[dict], str]:
    """One run's carbon arithmetic, and why it was excluded if it was.

    The admissibility rules (which tiers count, the assumed-power gate, the
    functional-unit requirement) live in :func:`sinter.sci.compute_sci`; this
    only supplies what is run-specific — the measured energy, the embodied
    method, and the token count.
    """
    energy = getattr(outcome, "energy", None) or {}
    tier = int(energy.get("tier") or 0)
    energy_kwh = energy.get("energy_kwh")
    if energy_kwh is None or tier == 0:
        return None, "energy_unavailable"

    block = _intensity_for(outcome, accounting)
    if block is None:
        detail = (getattr(outcome, "carbon", None) or {}).get("detail") or {}
        return None, detail.get("reason") or "intensity_unavailable"

    intensity = CarbonIntensity.from_dict(block)
    hours = max(float(getattr(outcome, "wall_seconds", 0.0)), 1e-9) / 3600.0
    embodied = compute_embodied(
        accounting=accounting,
        operational_kgco2e=float(energy_kwh) * intensity.kgco2e_per_kwh,
        workload_hours=hours)
    result = compute_sci(
        energy_kwh=float(energy_kwh),
        energy_tier=tier,
        intensity=intensity,
        embodied=embodied,
        functional_unit=FunctionalUnit(unit=UNIT_VERIFIED_TASK, count=1.0,
                                       label="verified task"),
        allow_assumed_power=bool(getattr(accounting, "allow_assumed_power",
                                         False)),
    )
    if result.sci is None:
        return None, result.reason or "sci_unavailable"

    tokens = int((getattr(outcome, "usage", None) or {}).get("total_tokens", 0))
    return {
        "energy_kwh": float(energy_kwh),
        "tier": tier,
        "operational_gco2e": result.operational_gco2e,
        "embodied_gco2e": result.embodied_gco2e,
        "total_gco2e": result.total_gco2e,
        "intensity": block,
        "embodied": result.embodied,
        "tokens": tokens,
        "verified": getattr(outcome, "status", "") == STS_VERIFIED,
        "series_sha256": ((getattr(outcome, "carbon", None) or {})
                          .get("detail") or {}).get("series_sha256", ""),
    }, ""


def _intensity_for(outcome, accounting) -> Optional[dict]:
    """This run's recorded intensity, or the static factor for old reports.

    A run that *refused* its intensity — ``require_hourly`` and no hourly
    series, or a provider with nothing usable — records ``intensity: null``.
    Substituting the annual factor here would re-defeat that refusal one layer
    above the code that implements it, so an explicit refusal is honoured and
    the run contributes no SCI.
    """
    carbon = getattr(outcome, "carbon", None) or {}
    if carbon:
        block = carbon.get("intensity")
        if isinstance(block, dict) and block.get("kgco2e_per_kwh") is not None:
            return block
        return None
    value = getattr(accounting, "emissions_factor_kgco2e_per_kwh", None)
    if value is None:
        return None
    return {
        "kgco2e_per_kwh": float(value),
        "source": getattr(accounting, "emissions_factor_source", "") or "static",
        "version": getattr(accounting, "emissions_factor_version", "") or "static",
        "basis": getattr(accounting, "emissions_intensity_basis", "location"),
        "region": getattr(accounting, "emissions_region", "") or "",
        "resolution_s": 0,
        "matched": "flat",
        "coverage": 0.0,
    }


def _derive_carbon(outcomes: list, accounting: Optional[object]
                   ) -> tuple[dict, CarbonBlock]:
    """Flat carbon metrics plus the provenance block that explains them."""
    empty = {key: None for key in CARBON_METRIC_KEYS}
    empty["energy_runs"] = 0

    tiers: dict[str, int] = {}
    for outcome in outcomes:
        tier = int(((getattr(outcome, "energy", None) or {}).get("tier") or 0))
        tiers[str(tier)] = tiers.get(str(tier), 0) + 1

    block = CarbonBlock(unit="verified task", tiers=tiers)
    if accounting is None:
        block.reason = "accounting_unconfigured"
        return empty, block

    runs = []
    excluded: list[str] = []
    for outcome in outcomes:
        entry, reason = _run_carbon(outcome, accounting)
        if entry is not None:
            runs.append(entry)
        elif reason:
            excluded.append(reason)
    if not runs:
        # Say which rule excluded the runs: "assumed power not allowed" and
        # "no energy at all" are different problems for the operator.
        block.reason = excluded[0] if excluded else "energy_unavailable"
        return empty, block

    verified_runs = [r for r in runs if r["verified"]]
    verified_total = sum(r["total_gco2e"] for r in verified_runs)
    all_total = sum(r["total_gco2e"] for r in runs)
    energy_kwh_total = sum(r["energy_kwh"] for r in runs)
    tokens_total = sum(r["tokens"] for r in runs)

    metrics = dict(empty)
    metrics["energy_kwh_total"] = round(energy_kwh_total, 6)
    metrics["operational_gco2e_total"] = round(
        sum(r["operational_gco2e"] for r in runs), 6)
    metrics["embodied_gco2e_total"] = round(
        sum(r["embodied_gco2e"] for r in runs), 6)
    metrics["energy_runs"] = len(runs)
    if verified_runs:
        metrics["sci_gco2e_per_verified_task"] = round(
            verified_total / len(verified_runs), 4)
        metrics["sci_gco2e_per_success"] = round(
            all_total / len(verified_runs), 4)
    if tokens_total > 0:
        metrics["sci_gco2e_per_1k_tokens"] = round(
            all_total / (tokens_total / 1000.0), 4)

    if metrics["sci_gco2e_per_verified_task"] is None:
        block.reason = "no_verified_runs"


    block.energy_quality = energy_quality_for_tier(max(r["tier"] for r in runs))
    block.intensity = _representative_intensity(runs)
    block.embodied = runs[-1]["embodied"]
    return metrics, block


def _representative_intensity(runs: list) -> Optional[dict]:
    """The intensity the report should present, preferring a time-resolved one."""
    resolved = [r for r in runs if r["intensity"].get("matched") == "hourly_weighted"]
    chosen = (resolved or runs)[0]["intensity"]
    block = dict(chosen)
    sources = sorted({r["intensity"].get("source") or "static" for r in runs})
    resolutions = sorted({int(r["intensity"].get("resolution_s") or 0)
                          for r in runs})
    hashes = sorted({r["series_sha256"] for r in runs if r["series_sha256"]})
    block["sources"] = sources
    block["resolutions"] = resolutions
    block["time_resolved_runs"] = len(resolved)
    if hashes:
        block["series_sha256"] = hashes[0]
    if len(sources) > 1 or len(resolutions) > 1:
        block["mixed"] = True
    return block


def _percentile(values: list, percent: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(float(ordered[0]), 3)
    position = (percent / 100.0) * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    value = ordered[low] + (ordered[high] - ordered[low]) * fraction
    return round(float(value), 3)


def _limitations(outcomes: Iterable) -> list[str]:
    limits = []
    for outcome in outcomes:
        for grade in outcome.grades:
            if grade.limitation:
                limits.append(f"{grade.grader_type}: {grade.limitation}")
    if any(o.status == STS_BREACHED for o in outcomes):
        limits.append(
            "breached runs prove nothing about task difficulty; the agent was "
            "stopped mid-flight")
    if any(o.status == STS_TIMEOUT for o in outcomes):
        limits.append(
            "timed-out runs are censored data, not failures of the same kind")
    limits.extend(_carbon_limitations(outcomes))
    return limits


def _carbon_limitations(outcomes: Iterable) -> list[str]:
    """Why the carbon numbers are worth exactly what they claim.

    These travel into the report the same way grader limitations do: a reader
    must not have to guess how much of an SCI figure is measurement.
    """
    limits: list[str] = []
    tiers = {}
    for outcome in outcomes:
        tier = int(((getattr(outcome, "energy", None) or {}).get("tier") or 0))
        tiers[tier] = tiers.get(tier, 0) + 1
    if not tiers:
        return limits
    if tiers.get(1) or tiers.get(2):
        limits.append(
            "energy was metered (tier 1/2) for some runs; the meter is on this "
            "machine, not on the workload alone")
    if tiers.get(3):
        limits.append(
            "energy was integrated from GPU power samples (tier 3, estimated); "
            "the sensor is device-wide, so other work on the same card is "
            "included")
    if tiers.get(4):
        limits.append(
            "energy for some runs used an assumed power figure (tier 4); it is "
            "a guess and is excluded from SCI unless allow_assumed_power is set")
    if tiers.get(0):
        limits.append(
            "no energy data was available for some runs; their carbon is "
            "reported as unavailable, never as zero")
    return limits


def _reproducible(environment: dict) -> bool:
    """A report is only reproducible when its lanes are known."""
    if not environment.get("lane_set_hash"):
        return False
    return not any("unresolved bundle" in warning
                   or "dead patch reference" in warning
                   for warning in environment.get("lane_warnings") or [])


def read_report(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read report {path}: {exc}") from exc


def validate_report(report: dict) -> list[str]:
    """Structural validation. Returns error strings; empty means valid."""
    errors: list[str] = []
    if not isinstance(report, dict):
        return ["report is not a JSON object"]
    if report.get("report_version") != REPORT_VERSION:
        errors.append(f"unsupported report_version: "
                      f"{report.get('report_version')!r}")
    for key in ("suite_id", "suite_fingerprint", "generated_at"):
        if not isinstance(report.get(key), str) or not report.get(key):
            errors.append(f"{key} must be a non-empty string")
    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        errors.append("metrics must be an object")
    else:
        for key in ("comparable_runs", "verified", "tokens_total",
                    "model_calls_total", *SAFETY_COUNTERS):
            value = metrics.get(key)
            if not isinstance(value, int) or isinstance(value, bool) \
                    or value < 0:
                errors.append(f"metrics.{key} must be an integer >= 0, "
                              f"got {value!r}")
        rate = metrics.get("verified_completion_rate")
        if not isinstance(rate, (int, float)) or isinstance(rate, bool) \
                or not 0.0 <= float(rate) <= 1.0:
            errors.append("metrics.verified_completion_rate must be in [0, 1]")
        # Carbon is validated only when present, so reports written before SCI
        # existed keep validating.
        if "carbon" in metrics:
            carbon = metrics.get("carbon")
            if not isinstance(carbon, dict):
                errors.append("metrics.carbon must be an object")
            elif not isinstance(carbon.get("principles"), list):
                errors.append("metrics.carbon.principles must be a list")
            for key in CARBON_METRIC_KEYS:
                if key not in metrics:
                    continue
                value = metrics.get(key)
                if value is None:
                    continue
                if not isinstance(value, (int, float)) or isinstance(value, bool) \
                        or not math.isfinite(value) or value < 0:
                    errors.append(
                        f"metrics.{key} must be a finite number >= 0 or null, "
                        f"got {value!r}")
    runs = report.get("runs")
    if not isinstance(runs, list):
        errors.append("runs must be a list")
    else:
        for run in runs:
            if not isinstance(run, dict) or \
                    not isinstance(run.get("task_id"), str) or \
                    not isinstance(run.get("status"), str):
                errors.append(f"bad run entry: {run!r}")
                break
    if not isinstance(report.get("limitations"), list):
        errors.append("limitations must be a list")
    return errors


def _format_carbon(metrics: dict) -> list[str]:
    """The carbon section: the number, its quality, and its provenance."""
    carbon = metrics.get("carbon")
    if not isinstance(carbon, dict):
        return []

    lines = [""] + format_sci_lines(metrics)
    principles = carbon.get("principles") or []
    if principles:
        lines.append("Green software principles:")
        for principle in principles:
            lines.append(f"  {principle.get('status'):8s} "
                         f"{principle.get('id')}: {principle.get('evidence')}")
    return lines


def format_report(report: dict) -> str:
    """Human-readable summary. States the caveats, not just the number."""
    report = report if isinstance(report, dict) else report.to_dict()
    metrics = report.get("metrics") or {}
    environment = report.get("environment") or {}
    lines = [
        f"Suite: {report.get('suite_id')} "
        f"({report.get('suite_fingerprint', '')[:12]})",
        f"Generated: {report.get('generated_at')}",
        f"Systems: {', '.join(s.get('name', '?') for s in report.get('systems') or [])}",
        "",
        f"Verified: {metrics.get('verified', 0)}/"
        f"{metrics.get('comparable_runs', 0)} "
        f"({(metrics.get('verified_completion_rate') or 0) * 100:.0f}%)",
        f"Wall time: {metrics.get('wall_seconds_total', 0)}s total, "
        f"p50 {metrics.get('wall_seconds_p50')}s",
        f"Tokens: {metrics.get('tokens_total', 0)} total, "
        f"p50 {metrics.get('tokens_p50')}",
        f"Model calls: {metrics.get('model_calls_total', 0)}",
    ]
    per_task = metrics.get("tokens_per_verified_task")
    if per_task is not None:
        lines.append(f"Tokens per verified task: {per_task}")
    per_success = metrics.get("tokens_per_success")
    if per_success is not None and per_success != per_task:
        lines.append(f"Tokens per success (all spend / wins): {per_success}")
    if metrics.get("timed_out"):
        lines.append(f"Timed out: {metrics['timed_out']}")
    if metrics.get("breached"):
        lines.append(f"Budget-breached: {metrics['breached']}")
    lines.extend(_format_carbon(metrics))
    lines.append("")
    lines.append("Runs:")
    for run in report.get("runs") or []:
        verdicts = ", ".join(f"{k}={v}" for k, v in
                             sorted((run.get("verdicts") or {}).items()))
        lines.append(f"  {run.get('task_id'):28s} {run.get('status'):9s} "
                     f"{run.get('wall_seconds')}s "
                     f"{run.get('usage', {}).get('total_tokens', 0)} tok"
                     + (f"  [{verdicts}]" if verdicts else ""))
    lines.append("")
    lines.append(f"Environment: lanes {environment.get('lane_set_hash', '?')[:12]}"
                 f" (reproducible: {environment.get('reproducible')})")
    for warning in environment.get("lane_warnings") or []:
        lines.append(f"  warn: {warning}")
    limitations = report.get("limitations") or []
    if limitations:
        lines.append("")
        lines.append("Limitations:")
        for limitation in limitations:
            lines.append(f"  - {limitation}")
    return "\n".join(lines)
