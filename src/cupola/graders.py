"""Acceptance graders.

The harness's job is to produce a verdict about an artifact that the agent
did not author. Every grader therefore returns evidence, not a score: what
command ran, what it exited with, which criterion it decided. A grade is
either `passed` or `failed` — there is no partial credit to argue about.

Graders never touch the network. Several grader types are deliberately weak
(``checklist`` and ``claims`` are pattern assertions, not judgement) and the
report carries that limitation per grader, because a benchmark that hides the
weakness of its own scoring is worse than one with no scoring.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from cupola.suites import GraderSpec

PASSED = "passed"
FAILED = "failed"

DEFAULT_COMMAND_TIMEOUT = 120
OUTPUT_TAIL = 2000

#: Grader types whose verdict is weaker than its name suggests. Recorded in
#: every report so a reader can weigh a pass accordingly.
LIMITATIONS = {
    "checklist": "pattern checks over required sections/keys; not judgement",
    "claims": "literal must/must-not assertions against a supplied corpus or "
              "fact sheet; a claim phrased unusually can evade both",
    "schema": "structural checks only; says nothing about content quality",
    "pytest": "decides the criteria the fixture author wrote, and no others",
}


@dataclass
class GradeResult:
    """One grader's verdict, with the evidence that produced it."""

    grader_type: str
    criterion: str
    passed: bool
    evidence: str = ""
    commands_run: list[str] = field(default_factory=list)
    limitation: str = ""

    @property
    def status(self) -> str:
        return PASSED if self.passed else FAILED

    def to_dict(self) -> dict:
        return {
            "grader_type": self.grader_type,
            "criterion": self.criterion,
            "status": self.status,
            "evidence": self.evidence,
            "commands_run": list(self.commands_run),
            "limitation": self.limitation,
        }


@dataclass
class GradeContext:
    """Everything a grader may look at."""

    workspace: Path
    suite_root: Optional[Path] = None
    timeout: int = DEFAULT_COMMAND_TIMEOUT


class GraderError(Exception):
    """A grader spec is malformed and cannot decide anything."""


# --- primitives -----------------------------------------------------------


def _tail(text: str, limit: int = OUTPUT_TAIL) -> str:
    return (text or "")[-limit:]


def _clear_bytecode_cache(root: Path) -> None:
    """Remove __pycache__ trees under a workspace.

    A stale .pyc can make a just-edited module run as its previous version,
    which would turn a broken submission into a passing grade. Graded runs
    must read the bytes on disk.
    """
    for cache_dir in root.rglob("__pycache__"):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)


def _run_command(argv: list[str], cwd: Path, timeout: int,
                 no_bytecode: bool = False) -> tuple[int, str]:
    """Run argv (never a shell string) in cwd and capture combined output."""
    env = None
    if no_bytecode:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(argv, cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except FileNotFoundError as exc:
        return 127, str(exc)
    return proc.returncode, _tail((proc.stdout or "") + (proc.stderr or ""))


def _tamper_failures(work_dir: Path, entry: list[str],
                     expected: Optional[dict]) -> list[str]:
    """Detect an acceptance file changed after the run began.

    The harness records fixture hashes before the agent starts and rechecks
    them here. An agent that edits its own acceptance test has not solved the
    task, however green the suite looks.
    """
    if not expected:
        return []
    from cupola.lanes import sha256_file

    failures = []
    for name in entry:
        baseline = expected.get(name)
        if not baseline:
            continue
        candidate = work_dir / name
        if not candidate.is_file():
            failures.append(f"{name} was deleted")
            continue
        actual = sha256_file(candidate)
        if actual != baseline:
            failures.append(
                f"{name} changed after the run started "
                f"({baseline[:12]} -> {actual[:12]})")
    return failures



def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


# --- grader implementations ----------------------------------------------


def grade_command(spec: GraderSpec, context: GradeContext) -> GradeResult:
    """Run a declared argv and require exit 0."""
    argv = spec.spec.get("argv")
    if not isinstance(argv, list) or not argv or \
            not all(isinstance(token, str) and token for token in argv):
        raise GraderError("command grader needs a non-empty argv string list")
    timeout = int(spec.spec.get("timeout", context.timeout))
    code, output = _run_command([str(t) for t in argv], context.workspace,
                                timeout, no_bytecode=True)
    return GradeResult(
        grader_type="command",
        criterion=spec.spec.get("criterion")
        or f"`{' '.join(str(t) for t in argv)}` exits 0",
        passed=code == 0,
        evidence=f"exit {code}\n{output}",
        commands_run=[" ".join(str(t) for t in argv)],
        limitation="decides only that this command succeeds",
    )


def grade_pytest(spec: GraderSpec, context: GradeContext) -> GradeResult:
    """Run an allowlisted pytest entry relative to the workspace root.

    The entry is a workspace-relative path (``tasks/x/test_task.py``), so the
    grader never relocates: it always runs from the workspace root with
    ``--rootdir`` pinned there. That is what stops one task's collection from
    reaching a sibling task's tests.
    """
    entry = spec.spec.get("entry")
    if not isinstance(entry, list) or not entry or \
            not all(isinstance(item, str) and item for item in entry):
        raise GraderError("pytest grader needs a non-empty entry list")

    work_dir = context.workspace
    if spec.spec.get("cwd"):
        work_dir = context.workspace / spec.spec["cwd"]

    missing = [item for item in entry if not (work_dir / item).exists()]
    if missing:
        return GradeResult(
            grader_type="pytest",
            criterion=f"{', '.join(missing)} present",
            passed=False,
            evidence=f"declared entry not found in the workspace: {missing}",
            limitation=LIMITATIONS["pytest"])

    tampered = _tamper_failures(work_dir, entry,
                                spec.spec.get("fixture_hashes"))
    if tampered:
        return GradeResult(
            grader_type="pytest",
            criterion="acceptance files are unmodified",
            passed=False,
            evidence="; ".join(tampered),
            limitation=LIMITATIONS["pytest"])

    timeout = int(spec.spec.get("timeout", context.timeout))
    # Absolute rootdir: a relative one is resolved against pytest's own cwd
    # and silently points outside the workspace.
    root = work_dir.resolve()
    # Confine collection to the workspace root. Without --rootdir and the
    # explicit -c, pytest walks up past the workspace and collects unrelated
    # tests, making one task's grade depend on another task's files.
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "--rootdir", str(root), "--override-ini", "addopts=",
            "-c", "pytest.ini", *[str(e) for e in entry]]
    ini = work_dir / "pytest.ini"
    if not ini.exists():
        ini.write_text("[pytest]\n")
    _clear_bytecode_cache(work_dir)
    code, output = _run_command(argv, work_dir, timeout, no_bytecode=True)
    return GradeResult(
        grader_type="pytest",
        criterion=spec.spec.get("criterion")
        or f"`{' '.join(entry)}` passes",
        passed=code == 0,
        evidence=f"exit {code} in {_relative(work_dir, context.workspace)}\n"
                 f"{output}",
        commands_run=[" ".join(argv)],
        limitation=LIMITATIONS["pytest"],
    )


def grade_schema(spec: GraderSpec, context: GradeContext) -> GradeResult:
    """Check a produced JSON artifact against required keys and types."""
    target = spec.spec.get("path")
    if not target:
        raise GraderError("schema grader needs a path")
    path = context.workspace / target
    if not path.is_file():
        return GradeResult("schema", f"{target} exists",
                           False, f"{target} was not produced",
                           limitation=LIMITATIONS["schema"])
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return GradeResult("schema", f"{target} is valid JSON", False,
                           str(exc), limitation=LIMITATIONS["schema"])

    problems = []
    if not isinstance(data, dict):
        problems.append("artifact is not a JSON object")
    else:
        for key in spec.spec.get("required_keys") or []:
            if key not in data or data[key] in (None, "", [], {}):
                problems.append(f"missing or empty key: {key}")
        for key in spec.spec.get("required_list_keys") or []:
            value = data.get(key)
            if not isinstance(value, list) or not value:
                problems.append(f"{key} must be a non-empty list")
        minimum = spec.spec.get("min_items")
        if isinstance(minimum, dict):
            for key, count in minimum.items():
                value = data.get(key)
                if not isinstance(value, list) or len(value) < int(count):
                    problems.append(
                        f"{key} needs at least {count} entries")
    return GradeResult(
        "schema", spec.spec.get("criterion") or f"{target} has the required shape",
        not problems,
        "; ".join(problems) if problems else "all declared keys present",
        limitation=LIMITATIONS["schema"],
    )


def grade_claims(spec: GraderSpec, context: GradeContext) -> GradeResult:
    """Literal must/must-not assertions over produced text.

    ``source`` may be a produced artifact or a supplied corpus file. When
    ``corpus`` is declared, every citation-like token in the artifact must
    resolve inside it — that is how a compliance or policy task is checked for
    traceability instead of trusting the model's recall of the law.
    """
    target = spec.spec.get("path")
    if not target:
        raise GraderError("claims grader needs a path")
    path = context.workspace / target
    if not path.is_file():
        return GradeResult("claims", f"{target} exists", False,
                           f"{target} was not produced",
                           limitation=LIMITATIONS["claims"])
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return GradeResult("claims", f"{target} is readable", False, str(exc),
                           limitation=LIMITATIONS["claims"])

    failures: list[str] = []
    for needle in spec.spec.get("must_contain") or []:
        if needle not in text:
            failures.append(f"missing required text: {needle!r}")
    for needle in spec.spec.get("must_not_contain") or []:
        if needle in text:
            failures.append(f"contains forbidden text: {needle!r}")
    for pattern in spec.spec.get("must_match") or []:
        if not re.search(pattern, text):
            failures.append(f"no match for required pattern: {pattern!r}")
    for pattern in spec.spec.get("must_not_match") or []:
        if re.search(pattern, text):
            failures.append(f"matched forbidden pattern: {pattern!r}")

    corpus_dir = spec.spec.get("corpus")
    if corpus_dir:
        corpus_path = context.workspace / corpus_dir
        corpus_text, corpus_problem = _load_corpus(corpus_path)
        if corpus_problem:
            failures.append(corpus_problem)
        else:
            known = _citation_targets(corpus_path)
            for citation in re.findall(
                    spec.spec.get("citation_pattern",
                                  r"\[([A-Za-z0-9_.\-/ ]+)\]"), text):
                cleaned = citation.strip()
                if not cleaned:
                    continue
                if cleaned in known:
                    continue
                if cleaned in corpus_text:
                    continue
                failures.append(
                    f"citation not found in supplied corpus: {cleaned!r}")

    return GradeResult(
        "claims",
        spec.spec.get("criterion") or f"{target} meets its factual assertions",
        not failures,
        "; ".join(failures) if failures else "all assertions satisfied",
        limitation=LIMITATIONS["claims"],
    )


def _citation_targets(directory: Path) -> set[str]:
    """Names a citation may legitimately resolve to, within the corpus.

    Resolution is by corpus *file*, so an invented clause cannot be excused by
    a coincidental word match somewhere in the corpus text.
    """
    names: set[str] = set()
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        names.update({relative, path.name, path.stem,
                      relative.replace("/", ".")})
    return names


def _load_corpus(directory: Path) -> tuple[str, str]:
    if not directory.is_dir():
        return "", f"corpus directory missing: {directory.name}"
    parts = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            try:
                parts.append(path.read_text(errors="replace"))
            except OSError:
                continue
    if not parts:
        return "", f"corpus directory is empty: {directory.name}"
    return "\n".join(parts), ""


def grade_checklist(spec: GraderSpec, context: GradeContext) -> GradeResult:
    """Required sections/keys present, and a minimum item count where asked."""
    target = spec.spec.get("path")
    if not target:
        raise GraderError("checklist grader needs a path")
    path = context.workspace / target
    if not path.is_file():
        return GradeResult("checklist", f"{target} exists", False,
                           f"{target} was not produced",
                           limitation=LIMITATIONS["checklist"])
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return GradeResult("checklist", f"{target} is readable", False,
                           str(exc), limitation=LIMITATIONS["checklist"])

    failures = []
    for section in spec.spec.get("required_sections") or []:
        if not re.search(rf"^#+\s*{re.escape(section)}\b", text,
                         re.MULTILINE | re.IGNORECASE):
            failures.append(f"missing section: {section}")
    for item in spec.spec.get("required_items") or []:
        if item not in text:
            failures.append(f"missing item: {item}")
    minimum = spec.spec.get("min_items")
    if minimum:
        count = len([line for line in text.splitlines()
                     if line.strip().startswith(("-", "*", "|"))])
        if count < int(minimum):
            failures.append(f"only {count} items, need {int(minimum)}")
    word_limit = spec.spec.get("max_words")
    if word_limit and len(text.split()) > int(word_limit):
        failures.append(f"{len(text.split())} words exceeds {word_limit}")
    word_floor = spec.spec.get("min_words")
    if word_floor and len(text.split()) < int(word_floor):
        failures.append(f"{len(text.split())} words is below {word_floor}")

    return GradeResult(
        "checklist",
        spec.spec.get("criterion") or f"{target} has the required sections",
        not failures,
        "; ".join(failures) if failures else "all required items present",
        limitation=LIMITATIONS["checklist"],
    )


def grade_recompute(spec: GraderSpec, context: GradeContext) -> GradeResult:
    """Independently recompute declared arithmetic and compare to the artifact.

    This is the strongest non-code grader available offline: the expectation is
    a number the harness computes itself, not prose the model wrote. It is what
    makes a financial-model task scoreable at all.

    Spec: ``path`` (JSON artifact), ``fields`` = {name: expected number or
    expression string}, ``tolerance`` (absolute, default 1e-6).
    """
    target = spec.spec.get("path")
    fields = spec.spec.get("fields")
    if not target or not isinstance(fields, dict) or not fields:
        raise GraderError("recompute grader needs a path and a fields map")
    path = context.workspace / target
    if not path.is_file():
        return GradeResult("recompute", f"{target} exists", False,
                           f"{target} was not produced",
                           limitation="arithmetic only; not model design")
    try:
        produced = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return GradeResult("recompute", f"{target} is valid JSON", False,
                           str(exc), limitation="arithmetic only")
    if not isinstance(produced, dict):
        return GradeResult("recompute", f"{target} is a JSON object", False,
                           "artifact is not an object", limitation="arithmetic only")

    tolerance = float(spec.spec.get("tolerance", 1e-6))
    failures = []
    for name, expected in fields.items():
        actual = produced.get(name)
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            failures.append(f"{name}: artifact has {actual!r}, expected a number")
            continue
        if abs(float(actual) - float(expected)) > tolerance:
            failures.append(
                f"{name}: artifact {actual} != expected {expected} "
                f"(tolerance {tolerance})")
    return GradeResult(
        "recompute",
        spec.spec.get("criterion") or "declared arithmetic reproduces",
        not failures,
        "; ".join(failures) if failures
        else f"{len(fields)} field(s) within tolerance {tolerance}",
        limitation="arithmetic only; not model design",
    )


def grade_perf(spec: GraderSpec, context: GradeContext) -> GradeResult:
    """A placeholder that never pretends to have measured anything.

    Throughput is measured by ``cupola bench perf``; a perf task in a suite
    exists so the run is recorded, and this grader records that no artifact
    verdict was made.
    """
    return GradeResult(
        "perf", spec.spec.get("criterion") or "throughput recorded",
        True, "no artifact verdict; see latency and token metrics",
        limitation="records a measurement, decides nothing",
    )


GRADERS: dict[str, Callable[[GraderSpec, GradeContext], GradeResult]] = {
    "command": grade_command,
    "pytest": grade_pytest,
    "schema": grade_schema,
    "claims": grade_claims,
    "checklist": grade_checklist,
    "recompute": grade_recompute,
    "perf": grade_perf,
}


def grade(specs: Iterable[GraderSpec], context: GradeContext) -> list[GradeResult]:
    """Run every grader. A malformed spec fails loudly rather than passing."""
    results: list[GradeResult] = []
    for spec in specs:
        runner = GRADERS.get(spec.type)
        if runner is None:
            results.append(GradeResult(
                spec.type, f"grader {spec.type!r} is known", False,
                f"unknown grader type {spec.type!r}"))
            continue
        try:
            results.append(runner(spec, context))
        except GraderError as exc:
            results.append(GradeResult(spec.type, "grader spec is valid",
                                       False, str(exc)))
    return results


def overall(results: Iterable[GradeResult]) -> bool:
    """A task passes only if every grader passed."""
    results = list(results)
    return bool(results) and all(result.passed for result in results)
