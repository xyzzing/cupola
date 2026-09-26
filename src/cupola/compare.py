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

from sinter.sci import carbon_principles

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
                   "timed_out", "breached", "errored", "failed",
                   "energy_kwh_total", "operational_gco2e_total",
                   "embodied_gco2e_total", "sci_gco2e_per_verified_task",
                   "sci_gco2e_per_success", "sci_gco2e_per_1k_tokens")

#: Carbon metrics whose delta is only meaningful when both arms used the same
#: methodology. Carbon is reported, never a gate: these deltas are annotated
#: rather than allowed to flip a verdict.
CARBON_DELTA_KEYS = (
    "energy_kwh_total",
    "operational_gco2e_total",
    "embodied_gco2e_total",
    "sci_gco2e_per_verified_task",
    "sci_gco2e_per_success",
    "sci_gco2e_per_1k_tokens",
)


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
    #: The carbon answer for this pair: the SCI delta, whether the two arms
    #: measured carbon the same way, and whether the candidate helped.
    carbon: dict = field(default_factory=dict)
    #: The candidate's green-software evidence, with this comparison filled in,
    #: so ``carbon_efficiency_proven`` can actually be earned.
    principles: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "baseline": self.baseline,
            "candidate": self.candidate,
            "deltas": self.deltas,
            "paired": self.paired,
            "carbon": dict(self.carbon),
            "principles": list(self.principles),
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
        # Deliberately no deltas: presenting a difference between two
        # non-comparable reports invites exactly the misuse the verdict warns
        # about.
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
        return _with_carbon_note(Comparison(
            label=label, verdict=VERDICT_FAIL, reasons=reasons),
            baseline, candidate)

    # 2. Sample size.
    runs = min(base.get("comparable_runs", 0), cand.get("comparable_runs", 0))
    if runs < MIN_COMPARABLE_RUNS:
        return _with_carbon_note(Comparison(
            label=label, verdict=VERDICT_INSUFFICIENT,
            reasons=[f"only {runs} comparable runs "
                     f"(need >= {MIN_COMPARABLE_RUNS}); a single run is not "
                     "a result"]), baseline, candidate)

    # 3. Completion drop.
    drop = base.get("verified_completion_rate", 0.0) - \
        cand.get("verified_completion_rate", 0.0)
    if drop > MAX_COMPLETION_DROP + _EPS:
        reasons.append(
            f"verified_completion_rate dropped {drop * 100:.1f}pp "
            f"({base.get('verified_completion_rate', 0):.1%} -> "
            f"{cand.get('verified_completion_rate', 0):.1%}, "
            f"limit {MAX_COMPLETION_DROP * 100:.0f}pp)")
        return _with_carbon_note(Comparison(
            label=label, verdict=VERDICT_FAIL, reasons=reasons),
            baseline, candidate)

    return _with_carbon_note(Comparison(
        label=label, verdict=VERDICT_PASS,
        reasons=["no protected metric regressed"]), baseline, candidate)


def _with_carbon_note(comparison: Comparison, baseline: dict,
                      candidate: dict) -> Comparison:
    """Attach deltas, then qualify the carbon ones if methodology differs.

    Carbon never changes the verdict: an estimate must not decide a pass or
    fail. But a delta computed across two different intensity methodologies is
    not a delta, so it is marked as such instead of being presented silently.
    """
    base = baseline.get("metrics") if isinstance(baseline.get("metrics"), dict) \
        else None
    cand = candidate.get("metrics") if isinstance(candidate.get("metrics"), dict) \
        else None
    if base is None or cand is None:
        # A report without metrics cannot be diffed; the existing verdict and
        # reasons stand (this is the fingerprint-mismatch path).
        return comparison
    comparison.deltas = _deltas(base, cand)
    comparison.paired = _paired(baseline, candidate)

    comparable, reason = _carbon_methodology(base, cand)
    if not comparable:
        for key in CARBON_DELTA_KEYS:
            if key in comparison.deltas:
                comparison.deltas[key]["comparable"] = False
        comparison.reasons.append(reason)
    comparison.carbon = _carbon_delta(base, cand, comparison.label, comparable,
                                      reason)
    comparison.principles = _principles_for(candidate, comparison.carbon)
    return comparison


def _principles_for(candidate: dict, carbon: dict) -> list[dict]:
    """The candidate's green-software evidence, with this A/B filled in.

    ``carbon_principles`` evaluates ``carbon.comparison`` for
    ``carbon_efficiency_proven``; a standalone report cannot know its own
    baseline, so the wiring happens here, where both arms exist.
    """
    metrics = candidate.get("metrics")
    if not isinstance(metrics, dict) or not isinstance(metrics.get("carbon"), dict):
        return []
    enriched = dict(metrics)
    enriched["carbon"] = {**metrics["carbon"], "comparison": dict(carbon)}
    return carbon_principles({"metrics": enriched})


def _carbon_delta(base: dict, cand: dict, label: str, methodology_ok: bool,
                  reason: str) -> dict:
    """The A/B carbon answer: did installing this actually help?

    ``helped`` is ``"unknown"`` when the comparison cannot support an answer —
    too few runs, no SCI on either side, or two different methodologies. A
    ``False`` is a real, publishable negative result, not an error.
    """
    left = base.get("sci_gco2e_per_verified_task")
    right = cand.get("sci_gco2e_per_verified_task")
    numeric = (isinstance(left, (int, float)) and isinstance(right, (int, float))
               and not isinstance(left, bool) and not isinstance(right, bool))
    delta = round(right - left, 4) if numeric else None
    completion_drop = (base.get("verified_completion_rate", 0.0) -
                       cand.get("verified_completion_rate", 0.0))
    completion_dropped = completion_drop > MAX_COMPLETION_DROP + _EPS
    enough = min(base.get("comparable_runs", 0),
                 cand.get("comparable_runs", 0)) >= MIN_COMPARABLE_RUNS
    if delta is None or not methodology_ok or not enough:
        helped = "unknown"
    else:
        helped = bool(delta <= 0 and not completion_dropped)
    return {
        "label": label,
        "comparable": bool(methodology_ok and enough and delta is not None),
        "methodology": "same" if methodology_ok else "differs",
        "sci_delta_gco2e_per_verified_task": delta,
        "completion_dropped": completion_dropped,
        "helped": helped,
        "reason": reason,
    }


def _carbon_methodology(base: dict, cand: dict) -> tuple[bool, str]:
    """Do both reports measure carbon the same way?

    Compares method, not data: two arms legitimately fetch different intervals,
    so the series hash is evidence for a reader, not a comparability gate.
    """
    left = base.get("carbon") or {}
    right = cand.get("carbon") or {}
    left_i = left.get("intensity") or {}
    right_i = right.get("intensity") or {}
    mismatches = []
    for name in ("source", "basis"):
        if left_i.get(name) != right_i.get(name):
            mismatches.append(
                f"{name} {left_i.get(name)!r} vs {right_i.get(name)!r}")
    if sorted(left_i.get("resolutions") or []) != \
            sorted(right_i.get("resolutions") or []):
        mismatches.append(
            f"resolution {left_i.get('resolutions')} vs "
            f"{right_i.get('resolutions')}")
    left_m = (left.get("embodied") or {}).get("method")
    right_m = (right.get("embodied") or {}).get("method")
    if left_m != right_m:
        mismatches.append(f"embodied method {left_m!r} vs {right_m!r}")
    if mismatches:
        return False, ("carbon methodology differs, so the carbon deltas are "
                       "marked not comparable: " + "; ".join(mismatches))
    return True, ""


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
                    "tokens_per_verified_task", "wall_seconds_p50",
                    "sci_gco2e_per_verified_task", "sci_gco2e_per_1k_tokens",
                    "energy_kwh_total"):
            entry = deltas.get(key)
            if entry:
                direction = "better" if entry["better"] else "worse"
                suffix = "" if entry.get("comparable", True) \
                    else " (not comparable: methodology differs)"
                lines.append(f"    {key}: {entry['baseline']} -> "
                             f"{entry['candidate']} ({direction}){suffix}")
    for note in attribution.get("notes") or []:
        lines.append(f"note: {note}")
    carbon_lines = [
        _format_comparison_carbon(c.get("carbon") or {})
        for c in attribution.get("comparisons") or []
    ]
    lines.extend(line for line in carbon_lines if line)
    principle_lines = [
        _format_comparison_principles(c.get("principles") or [])
        for c in attribution.get("comparisons") or []
    ]
    lines.extend(line for line in principle_lines if line)
    return "\n".join(lines)


def _format_comparison_principles(principles: list) -> str:
    """One compact line: what was earned, and what is still unproven."""
    passing = [p.get("id") for p in principles if p.get("status") == "pass"]
    unproven = [p.get("id") for p in principles if p.get("status") != "pass"]
    if not passing and not unproven:
        return ""
    text = f"green evidence: {len(passing)}/{len(principles)} principles proven"
    if passing:
        text += f" [{', '.join(passing)}]"
    if unproven:
        text += f"; unproven: {', '.join(unproven)}"
    return text


def _format_comparison_carbon(carbon: dict) -> str:
    if not carbon:
        return ""
    helped = carbon.get("helped")
    delta = carbon.get("sci_delta_gco2e_per_verified_task")
    if helped == "unknown":
        return ("carbon: unknown - " +
                (carbon.get("reason") or "the comparison cannot support an "
                 "answer"))
    verdict = "helping" if helped else "not helping"
    text = (f"carbon: {verdict} - SCI delta {delta} gCO2e per verified task")
    if carbon.get("completion_dropped"):
        text += " (completion rate dropped: the win is not a real one)"
    return text
