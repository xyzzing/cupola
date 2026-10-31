"""Compare benchmark reports, and attribute the difference to lanes.

Two reports may only be compared when their suite fingerprints match. Beyond
that, three questions are worth asking, and they are different questions:

``total``
    The full lane set versus none. Answers "what does my stack buy me?"
``marginal``
    Each lane toggled alone against the empty baseline. Answers "what does
    this plugin buy me?", which is the question that matters as plugins
    accumulate.
``interaction``
    A declared group against each member alone. Answers "do these two only
    work together?" Pairwise only: N lanes make a full factorial schedule
    explode, so the cap returns INSUFFICIENT_SAMPLE with the cost rather than
    silently running for hours.

Verdict precedence is inherited from the comparator already proven in
``minder_op/benchmark.py``: non-comparability first, then absolute safety
regressions, then sample size, then a regression threshold. Safety beats
sample size; sample size beats noise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_INSUFFICIENT = "INSUFFICIENT_SAMPLE"
VERDICT_NON_COMPARABLE = "NON_COMPARABLE"

ATTRIBUTION_MODES = ("total", "marginal", "interaction")

#: Below this many comparable runs a difference is not a result.
MIN_COMPARABLE_RUNS = 5
#: A completion drop larger than this many percentage points fails.
MAX_COMPLETION_DROP = 0.05
#: Above this many lanes, interaction attribution is refused as too expensive.
MAX_INTERACTION_LANES = 6

_EPS = 1e-9

#: Metrics where a larger value is worse.
LOWER_IS_BETTER = ("wall_seconds_p50", "tokens_p50", "tokens_total",
                   "tokens_per_success", "tokens_per_verified_task",
                   "wall_seconds_per_success", "wall_seconds_total",
                   "output_tokens_total", "model_calls_total",
                   "timed_out", "breached", "errored", "failed")


class CompareError(Exception):
    """Reports cannot be compared (unreadable, wrong shape)."""


@dataclass
class Comparison:
    """One pairwise comparison's outcome."""

    label: str
    verdict: str
    reasons: list[str] = field(default_factory=list)
    baseline: Optional[str] = None
    candidate: Optional[str] = None
    deltas: dict = field(default_factory=dict)
    paired: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "baseline": self.baseline,
            "candidate": self.candidate,
            "deltas": self.deltas,
            "paired": self.paired,
        }


def load_reports(paths: Iterable[Path]) -> list[dict]:
    reports = []
    for path in paths:
        try:
            reports.append(json.loads(Path(path).read_text()))
        except (OSError, ValueError) as exc:
            raise CompareError(f"cannot read report {path}: {exc}") from exc
    return reports


def _metrics(report: dict) -> dict:
    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        raise CompareError("report has no metrics object")
    return metrics


def compare_pair(baseline: dict, candidate: dict,
                 label: str = "comparison") -> Comparison:
    """Compare two reports, applying verdict precedence."""
    if baseline.get("suite_fingerprint") != candidate.get("suite_fingerprint"):
        return Comparison(
            label=label, verdict=VERDICT_NON_COMPARABLE,
            reasons=[
                "suite_fingerprint mismatch: the task set changed, so the "
                f"numbers are not comparable "
                f"({str(baseline.get('suite_fingerprint'))[:12]} vs "
                f"{str(candidate.get('suite_fingerprint'))[:12]})"],
        )

    base = _metrics(baseline)
    cand = _metrics(candidate)
    reasons: list[str] = []

    # 1. Absolute safety regressions fail at any sample size.
    for key in ("unsafe_executions", "prohibited_action_attempts",
                "reproducibility_violations"):
        if cand.get(key, 0) > 0:
            reasons.append(
                f"{key}: candidate {cand.get(key)} > 0 "
                "(absolute regression, any sample size)")
    if reasons:
        return Comparison(label=label, verdict=VERDICT_FAIL, reasons=reasons,
                          deltas=_deltas(base, cand),
                          paired=_paired(baseline, candidate))

    # 2. Sample size.
    runs = min(base.get("comparable_runs", 0), cand.get("comparable_runs", 0))
    if runs < MIN_COMPARABLE_RUNS:
        return Comparison(
            label=label, verdict=VERDICT_INSUFFICIENT,
            reasons=[f"only {runs} comparable runs "
                     f"(need >= {MIN_COMPARABLE_RUNS}); a single run is not "
                     "a result"],
            deltas=_deltas(base, cand), paired=_paired(baseline, candidate))

    # 3. Completion drop.
    drop = base.get("verified_completion_rate", 0.0) - \
        cand.get("verified_completion_rate", 0.0)
    if drop > MAX_COMPLETION_DROP + _EPS:
        reasons.append(
            f"verified_completion_rate dropped {drop * 100:.1f}pp "
            f"({base.get('verified_completion_rate', 0):.1%} -> "
            f"{cand.get('verified_completion_rate', 0):.1%}, "
            f"limit {MAX_COMPLETION_DROP * 100:.0f}pp)")
        return Comparison(label=label, verdict=VERDICT_FAIL, reasons=reasons,
                          deltas=_deltas(base, cand),
                          paired=_paired(baseline, candidate))

    return Comparison(label=label, verdict=VERDICT_PASS,
                      reasons=["no protected metric regressed"],
                      deltas=_deltas(base, cand),
                      paired=_paired(baseline, candidate))


def _deltas(base: dict, cand: dict) -> dict:
    keys = sorted(set(base) | set(cand))
    deltas = {}
    for key in keys:
        left, right = base.get(key), cand.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)) \
                and not isinstance(left, bool) and not isinstance(right, bool):
            deltas[key] = {
                "baseline": left,
                "candidate": right,
                "delta": round(right - left, 4),
                "better": (right < left) if key in LOWER_IS_BETTER
                else (right > left),
            }
    return deltas


def _paired(baseline: dict, candidate: dict) -> dict:
    """Per-task paired comparison: the same task under both arms.

    Pairing removes task difficulty from the difference, which is the whole
    reason an A/B on a handful of tasks can say anything at all.
    """
    base_runs = {run.get("task_id"): run for run in baseline.get("runs") or []}
    cand_runs = {run.get("task_id"): run for run in candidate.get("runs") or []}
    shared = sorted(set(base_runs) & set(cand_runs))
    if not shared:
        return {"shared_tasks": 0, "note": "no shared tasks to pair"}

    won = lost = same = 0
    token_deltas = []
    for task_id in shared:
        left, right = base_runs[task_id], cand_runs[task_id]
        left_ok = left.get("status") == "verified"
        right_ok = right.get("status") == "verified"
        if left_ok and not right_ok:
            lost += 1
        elif right_ok and not left_ok:
            won += 1
        else:
            same += 1
        left_tokens = (left.get("usage") or {}).get("total_tokens")
        right_tokens = (right.get("usage") or {}).get("total_tokens")
        if isinstance(left_tokens, int) and isinstance(right_tokens, int):
            token_deltas.append(right_tokens - left_tokens)

    summary = {
        "shared_tasks": len(shared),
        "regressed": lost,
        "improved": won,
        "unchanged": same,
    }
    if token_deltas:
        summary["mean_token_delta"] = round(
            sum(token_deltas) / len(token_deltas), 1)
    return summary


# --- attribution ----------------------------------------------------------


@dataclass
class Attribution:
    """A whole attribution result across several reports."""

    mode: str
    comparisons: list[Comparison] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "comparisons": [c.to_dict() for c in self.comparisons],
            "notes": list(self.notes),
        }


def attribute_total(baseline: dict, full: dict) -> Attribution:
    """Full lane set against the empty baseline."""
    return Attribution(
        mode="total",
        comparisons=[compare_pair(baseline, full,
                                  label="full-lane-set vs no-lanes")],
        notes=["measures the whole stack, not any single plugin"])


def attribute_marginal(baseline: dict,
                       arms: dict[str, dict]) -> Attribution:
    """Each lane alone against the empty baseline."""
    comparisons = [compare_pair(baseline, report, label=f"{lane} vs none")
                   for lane, report in sorted(arms.items())]
    return Attribution(
        mode="marginal", comparisons=comparisons,
        notes=["each lane measured alone, so effects do not alias each other"])


def attribute_interaction(members: dict[str, dict],
                          group: Optional[dict] = None,
                          cap: int = MAX_INTERACTION_LANES) -> Attribution:
    """Group against each member alone: does the effect need both?"""
    if len(members) > cap:
        return Attribution(
            mode="interaction", comparisons=[],
            notes=[f"{len(members)} lanes exceeds the pairwise cap of {cap}: "
                   f"a full schedule would need "
                   f"{2 ** len(members)} arms. Declare a smaller group, or "
                   "use marginal attribution."])
    if group is None:
        return Attribution(
            mode="interaction", comparisons=[],
            notes=["no group arm supplied; interaction needs the combined "
                   "lane set as well as each member alone"])

    comparisons = []
    for lane, report in sorted(members.items()):
        comparisons.append(compare_pair(
            report, group, label=f"group vs {lane} alone"))
    notes = ["an interaction shows only when a lane's effect depends on "
             "another lane being present"]
    if len(members) < 2:
        notes.append("a single member cannot show an interaction")
    return Attribution(mode="interaction", comparisons=comparisons,
                       notes=notes)


def format_attribution(attribution: dict) -> str:
    lines = [f"Attribution mode: {attribution.get('mode')}", ""]
    for comparison in attribution.get("comparisons") or []:
        lines.append(f"{comparison['label']}: {comparison['verdict']}")
        for reason in comparison.get("reasons") or []:
            lines.append(f"    {reason}")
        paired = comparison.get("paired") or {}
        if paired.get("shared_tasks"):
            lines.append(
                f"    paired: {paired['shared_tasks']} tasks, "
                f"{paired.get('improved', 0)} improved, "
                f"{paired.get('regressed', 0)} regressed, "
                f"{paired.get('unchanged', 0)} unchanged")
        deltas = comparison.get("deltas") or {}
        for key in ("verified_completion_rate", "tokens_per_success",
                    "tokens_per_verified_task", "wall_seconds_p50"):
            entry = deltas.get(key)
            if entry:
                direction = "better" if entry["better"] else "worse"
                lines.append(f"    {key}: {entry['baseline']} -> "
                             f"{entry['candidate']} ({direction})")
    for note in attribution.get("notes") or []:
        lines.append(f"note: {note}")
    return "\n".join(lines)
