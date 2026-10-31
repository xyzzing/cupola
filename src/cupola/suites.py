"""Benchmark suite manifests for the Compass harness.

A suite is a versioned manifest plus per-task fixtures. The manifest format
follows the one already proven in ``minder/benchmarks`` — ``schema_version``,
``suite_id``, ``domain``, and a ``tasks`` list — extended with what an
end-to-end run needs: a kickoff prompt, graders, budgets and lane
requirements.

Comparability rule (inherited): the fingerprint covers the *functional* core
only, so editing a title keeps reports comparable while changing a task does
not. Two reports may only be compared when their fingerprints match.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1

#: Domain vocabulary. Adding a domain is a deliberate act: each one needs
#: graders that score an artifact the model cannot author.
DOMAINS = ("coding", "trading_research", "resume", "risk_management",
           "compliance_trade", "compliance_banking", "policy_analysis",
           "financial_model")

#: Task kinds are the same closed set as the domains, plus direct
#: model-performance tasks that produce no graded artifact.
TASK_KINDS = DOMAINS + ("perf",)

GRADER_TYPES = ("pytest", "checklist", "claims", "schema", "recompute",
                "command", "perf")

#: Capabilities a task must never exercise. Enforced by construction at run
#: time; declared per task so the manifest states it explicitly.
FORBIDDEN_VOCABULARY = ("network", "browser", "broker", "ssh",
                        "package_install", "frontier_call",
                        "writes_outside_workspace")

EXPECTED_VALUES = ("tests_pass", "artifact_matches", "manual_review")

DEFAULT_WALL_SECONDS = 1800
DEFAULT_TOKEN_BUDGET = 60000
DEFAULT_MAX_TURNS = 40


class SuiteError(Exception):
    """Suite or task cannot be loaded (missing, unreadable, bad JSON)."""


def benchmarks_root(override: Optional[Path] = None) -> Path:
    if override is not None:
        return Path(override)
    env = os.environ.get("CUPOLA_BENCHMARKS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "benchmarks"


@dataclass
class GraderSpec:
    """One acceptance check, run by the harness, never by the agent."""

    type: str
    spec: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type, **self.spec}


@dataclass
class Budgets:
    """Hard limits; exceeding any of them terminates the run."""

    wall_seconds: int = DEFAULT_WALL_SECONDS
    token_budget: int = DEFAULT_TOKEN_BUDGET
    max_turns: int = DEFAULT_MAX_TURNS

    def to_dict(self) -> dict:
        return {"wall_seconds": self.wall_seconds,
                "token_budget": self.token_budget,
                "max_turns": self.max_turns}


@dataclass
class Task:
    """One benchmark task: a prompt, its fixtures, and its acceptance."""

    task_id: str
    kind: str
    title: str = ""
    kickoff: str = ""
    fixtures: list[str] = field(default_factory=list)
    graders: list[GraderSpec] = field(default_factory=list)
    budgets: Budgets = field(default_factory=Budgets)
    expected: str = "tests_pass"
    forbidden: list[str] = field(default_factory=lambda: list(
        FORBIDDEN_VOCABULARY))
    lane_requirements: list[str] = field(default_factory=list)
    reference: Optional[str] = None
    corpus: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "title": self.title,
            "kickoff": self.kickoff,
            "fixtures": list(self.fixtures),
            "graders": [grader.to_dict() for grader in self.graders],
            "budgets": self.budgets.to_dict(),
            "expected": self.expected,
            "forbidden": list(self.forbidden),
            "lane_requirements": list(self.lane_requirements),
            "reference": self.reference,
            "corpus": list(self.corpus),
        }


@dataclass
class Suite:
    """A loaded suite plus the result of validating it against its directory."""

    suite_id: str
    domain: str = ""
    title: str = ""
    description: str = ""
    tasks: list[Task] = field(default_factory=list)
    root: Optional[Path] = None
    schema_version: int = SCHEMA_VERSION
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "suite_id": self.suite_id,
            "domain": self.domain,
            "title": self.title,
            "description": self.description,
            "tasks": [task.to_dict() for task in self.tasks],
            "errors": list(self.errors),
            "suite_fingerprint": self.fingerprint(),
        }

    def fingerprint(self) -> str:
        """Hash of the functional core: cosmetic edits stay comparable."""
        core = {
            "schema_version": self.schema_version,
            "suite_id": self.suite_id,
            "domain": self.domain,
            "tasks": [_fingerprint_task(task) for task in self.tasks],
        }
        return hashlib.sha256(_canonical(core).encode("utf-8")).hexdigest()

    def task(self, task_id: str) -> Optional[Task]:
        return next((t for t in self.tasks if t.task_id == task_id), None)


def _fingerprint_task(task: Task) -> dict:
    """Task identity minus prose: title is cosmetic, everything else is not."""
    data = task.to_dict()
    data.pop("title", None)
    return data


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _manifest_path(suite_id: str, root: Optional[Path] = None) -> Path:
    base = benchmarks_root(root) / suite_id
    for name in ("manifest.json", "suite.json"):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return base / "manifest.json"


def load_suite(suite_id: str, root: Optional[Path] = None) -> Suite:
    """Load a suite manifest and validate it against its directory."""
    path = _manifest_path(suite_id, root)
    if not path.is_file():
        raise SuiteError(f"unknown suite '{suite_id}' (no manifest at {path})")
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SuiteError(f"cannot read suite '{suite_id}': {exc}") from exc
    if not isinstance(raw, dict):
        raise SuiteError(f"manifest for '{suite_id}' is not a JSON object")

    suite_dir = path.parent
    tasks = [_task_from_raw(item, suite_dir)
             for item in raw.get("tasks") or [] if isinstance(item, dict)]
    suite = Suite(
        suite_id=str(raw.get("suite_id") or suite_dir.name),
        domain=str(raw.get("domain") or ""),
        title=str(raw.get("title") or ""),
        description=str(raw.get("description") or ""),
        tasks=tasks,
        root=suite_dir,
        schema_version=raw.get("schema_version", SCHEMA_VERSION),
    )
    suite.errors = validate_suite(suite, raw)
    return suite


def _task_from_raw(raw: dict, suite_dir: Path) -> Task:
    budgets_raw = raw.get("budgets") or {}
    graders = []
    for item in raw.get("graders") or []:
        if isinstance(item, dict):
            kind = str(item.get("type") or "")
            # Copy: the caller's manifest object stays untouched.
            spec = {key: value for key, value in item.items() if key != "type"}
            graders.append(GraderSpec(type=kind, spec=spec))
        elif isinstance(item, str):
            graders.append(GraderSpec(type=item))
    return Task(
        task_id=str(raw.get("task_id") or ""),
        kind=str(raw.get("kind") or ""),
        title=str(raw.get("title") or ""),
        kickoff=str(raw.get("kickoff") or ""),
        fixtures=[str(f) for f in raw.get("fixtures") or []],
        graders=graders,
        budgets=Budgets(
            wall_seconds=int(budgets_raw.get("wall_seconds",
                                             DEFAULT_WALL_SECONDS)),
            token_budget=int(budgets_raw.get("token_budget",
                                             DEFAULT_TOKEN_BUDGET)),
            max_turns=int(budgets_raw.get("max_turns", DEFAULT_MAX_TURNS)),
        ),
        expected=str(raw.get("expected") or "tests_pass"),
        forbidden=[str(f) for f in raw.get("forbidden")
                   or list(FORBIDDEN_VOCABULARY)],
        lane_requirements=[str(x) for x in raw.get("lane_requirements") or []],
        reference=raw.get("reference"),
        corpus=[str(c) for c in raw.get("corpus") or []],
    )


def validate_suite(suite: Suite, raw: Optional[dict] = None) -> list[str]:
    """Structural validation. Returns error strings; empty means valid."""
    errors: list[str] = []
    if suite.schema_version != SCHEMA_VERSION:
        errors.append(f"unsupported schema_version: {suite.schema_version!r}")
    if not suite.suite_id:
        errors.append("suite_id must be a non-empty string")
    if suite.domain not in DOMAINS:
        errors.append(f"domain must be one of {DOMAINS}, "
                      f"got {suite.domain!r}")
    if not suite.tasks:
        errors.append("tasks must be a non-empty list")
        return errors

    seen: set[str] = set()
    for task in suite.tasks:
        label = task.task_id or "<missing task_id>"
        if not task.task_id:
            errors.append("every task needs a non-empty task_id")
        elif task.task_id in seen:
            errors.append(f"duplicate task_id: {task.task_id}")
        else:
            seen.add(task.task_id)
        if task.kind not in TASK_KINDS:
            errors.append(f"{label}: kind must be one of {TASK_KINDS}, "
                          f"got {task.kind!r}")
        elif suite.domain and task.kind not in ("perf", suite.domain):
            errors.append(f"{label}: kind {task.kind!r} is outside the "
                          f"suite domain {suite.domain!r}")
        if task.expected not in EXPECTED_VALUES:
            errors.append(f"{label}: expected must be one of "
                          f"{EXPECTED_VALUES}, got {task.expected!r}")
        if not task.kickoff:
            errors.append(f"{label}: kickoff prompt is required")
        unknown = sorted(set(task.forbidden) - set(FORBIDDEN_VOCABULARY))
        if unknown:
            errors.append(f"{label}: forbidden outside the closed "
                          f"vocabulary: {unknown}")
        if not task.graders:
            errors.append(f"{label}: at least one grader is required")
        for grader in task.graders:
            if grader.type not in GRADER_TYPES:
                errors.append(f"{label}: unknown grader type "
                              f"{grader.type!r}")
        for budget_name in ("wall_seconds", "token_budget", "max_turns"):
            if getattr(task.budgets, budget_name) <= 0:
                errors.append(f"{label}: budgets.{budget_name} must be > 0")
        errors.extend(_missing_paths(task, suite.root))
    return errors


def _missing_paths(task: Task, suite_root: Optional[Path]) -> list[str]:
    if suite_root is None:
        return []
    errors = []
    for group in (task.fixtures, task.corpus):
        for rel in group:
            if not (suite_root / rel).exists():
                errors.append(f"{task.task_id}: missing path {rel}")
    if task.kickoff and not (suite_root / task.kickoff).is_file():
        errors.append(f"{task.task_id}: missing kickoff file {task.kickoff}")
    if task.reference and not (suite_root / task.reference).exists():
        errors.append(f"{task.task_id}: missing reference {task.reference}")
    return errors


def list_suites(root: Optional[Path] = None) -> list[dict]:
    """Every discoverable suite with a one-line status. Never raises."""
    base = benchmarks_root(root)
    rows: list[dict] = []
    if not base.is_dir():
        return rows
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        try:
            suite = load_suite(entry.name, base)
        except SuiteError as exc:
            rows.append({"suite_id": entry.name, "domain": "-", "tasks": 0,
                         "fingerprint": "-", "status": f"invalid: {exc}"})
            continue
        rows.append({
            "suite_id": suite.suite_id,
            "domain": suite.domain,
            "tasks": len(suite.tasks),
            "fingerprint": suite.fingerprint()[:12],
            "status": "ok" if not suite.errors
                      else f"invalid: {'; '.join(suite.errors)}",
        })
    return rows


def plan_run(suite: Suite, task_id: Optional[str] = None,
             system: Optional[str] = None, runs: int = 1) -> dict:
    """What a real run would do. Execution lands in the next milestone."""
    selected = suite.tasks
    notes: list[str] = []
    if task_id:
        match = suite.task(task_id)
        if match is None:
            notes.append(f"unknown task {task_id!r}; nothing selected")
            selected = []
        else:
            selected = [match]
    planned = []
    for task in selected:
        planned.append({
            "task_id": task.task_id,
            "kind": task.kind,
            "graders": [grader.type for grader in task.graders],
            "budgets": task.budgets.to_dict(),
            "forbidden": list(task.forbidden),
            "lane_requirements": list(task.lane_requirements),
        })
    if not suite.errors and not system:
        notes.append("no --system given: execution needs one")
    if suite.errors:
        notes.append("suite is invalid; fix the errors before running")
    return {
        "suite_id": suite.suite_id,
        "domain": suite.domain,
        "suite_fingerprint": suite.fingerprint(),
        "system": system,
        "runs": runs,
        "dry_run": True,
        "estimated_sessions": len(planned) * max(runs, 1),
        "tasks": planned,
        "writes": "nothing: dry-run only",
        "notes": notes,
    }
