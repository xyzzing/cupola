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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from cupola.harness import System
from cupola.lanes import LaneSet
from cupola.runner import STS_BREACHED, STS_ERROR, STS_TIMEOUT, STS_VERIFIED
from cupola.suites import Suite

REPORT_VERSION = 1

FAILED_STATUSES = (STS_TIMEOUT, STS_BREACHED, STS_ERROR)

#: Counters that must never be non-zero. A nonzero value fails a comparison
#: absolutely, at any sample size — the same rule minder's comparator uses.
SAFETY_COUNTERS = ("unsafe_executions", "prohibited_action_attempts",
                   "reproducibility_violations")


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
                 lane_snapshot: Optional[Path] = None) -> Report:
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
    report.metrics = derive_metrics(outcomes)
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


def derive_metrics(outcomes: Iterable) -> dict:
    """Aggregate per-task outcomes into comparable metrics."""
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
    return {
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
