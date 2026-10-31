"""Execute one benchmark task and grade it.

The runner is deliberately dumb about models: it stages a fresh workspace,
launches the system under test with hard budgets, records what happened, and
hands the result to independent graders. It never asks the agent whether it
succeeded.

Isolation and honesty rules:

* A fresh temporary workspace per task, so a previous task's edits cannot
  explain a pass.
* Fixtures are hashed before the agent starts and rechecked at grade time, so
  greening a suite by rewriting it fails instead of passing.
* Budgets are enforced *during* the run, not measured after it. A wall-clock
  kill and a token kill are different outcomes and are reported differently;
  a post-hoc check cannot stop a runaway.
* Every outcome is written to disk before anything else can fail, so a run
  that dies leaves evidence rather than nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cupola.graders import GradeContext, GradeResult, overall
from cupola.harness import System, build_argv, resolved_binary
from cupola.suites import Task, load_suite
from cupola.transcript import newest_session_log, parse_session

STS_VERIFIED = "verified"
STS_FAILED = "failed"
STS_TIMEOUT = "timeout"
STS_BREACHED = "breached"
STS_ERROR = "error"

TERMINAL_STATUSES = (STS_VERIFIED, STS_FAILED, STS_TIMEOUT, STS_BREACHED,
                     STS_ERROR)

TOKEN_POLL_SECONDS = 5.0
OUTPUT_TAIL_BYTES = 4000


class RunnerError(Exception):
    """The run cannot be attempted (bad system, missing suite, no workspace)."""


@dataclass
class RunnerConfig:
    """Where state lives and how patient the runner is."""

    state_dir: Optional[Path] = None
    sessions_root: Optional[Path] = None
    keep_workspace: bool = False
    token_poll_seconds: float = TOKEN_POLL_SECONDS
    extra_env: dict = field(default_factory=dict)

    def resolved_state_dir(self) -> Path:
        if self.state_dir is not None:
            return Path(self.state_dir)
        env = os.environ.get("CUPOLA_BENCH_STATE")
        if env:
            return Path(env)
        from cupola.config import DEFAULT_STATE_DIR
        return DEFAULT_STATE_DIR / "bench"

    def resolved_sessions_root(self) -> Path:
        if self.sessions_root is not None:
            return Path(self.sessions_root)
        env = os.environ.get("CUPOLA_DSH_SESSIONS")
        if env:
            return Path(env)
        return Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh") \
            / "sessions"

    def child_env(self) -> dict:
        """Environment for the spawned agent.

        Sets CUPOLA_DSH_SESSIONS so a harness writes its session log where the
        budget watcher will look for it, whichever DSH_HOME it resolves.
        """
        env = {**self.extra_env}
        env.setdefault("CUPOLA_DSH_SESSIONS",
                       str(self.resolved_sessions_root()))
        return env


@dataclass
class TaskOutcome:
    """Everything one task run produced, ready to become report rows."""

    task_id: str
    system: str
    status: str
    run_id: str = ""
    exit_code: Optional[int] = None
    wall_seconds: float = 0.0
    grades: list[GradeResult] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    engine: dict = field(default_factory=dict)
    workspace: Optional[str] = None
    log_path: Optional[str] = None
    problems: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == STS_VERIFIED

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "system": self.system,
            "status": self.status,
            "passed": self.passed,
            "run_id": self.run_id,
            "exit_code": self.exit_code,
            "wall_seconds": round(self.wall_seconds, 3),
            "usage": dict(self.usage),
            "engine": dict(self.engine),
            "workspace": self.workspace,
            "log_path": self.log_path,
            "problems": list(self.problems),
            "grades": [result.to_dict() for result in self.grades],
        }


# --- staging --------------------------------------------------------------


def stage_workspace(suite_root: Path, task: Task,
                    into: Optional[Path] = None) -> Path:
    """Copy a task's fixtures into a fresh workspace.

    Fixture paths are copied preserving their relative layout, so a task that
    declares ``tasks/x/solution/task.py`` gets that path in the workspace.
    """
    # Absolute from the start: a relative workspace breaks both the spawned
    # agent (cwd is applied by the OS) and any grader argv that embeds a path.
    workspace = (Path(into) if into else Path(tempfile.mkdtemp(
        prefix=f"bench-{task.task_id}-"))).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    for rel in task.fixtures:
        source = suite_root / rel
        if not source.exists():
            raise RunnerError(f"fixture missing for {task.task_id}: {rel}")
        target = workspace / rel
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return workspace


def fixture_hashes(workspace: Path, task: Task) -> dict:
    """Hash the acceptance files a task must not modify."""
    from cupola.lanes import sha256_file

    hashes: dict[str, str] = {}
    for grader in task.graders:
        if grader.type != "pytest":
            continue
        for entry in grader.spec.get("entry") or []:
            matches = sorted(workspace.rglob(entry))
            for match in matches:
                hashes[match.relative_to(workspace).as_posix()] = \
                    sha256_file(match)
    return hashes


def _bind_fixture_hashes(task: Task, hashes: dict) -> Task:
    """Attach pre-run hashes to the pytest graders of a task copy."""
    from cupola.suites import GraderSpec

    graders = []
    for grader in task.graders:
        if grader.type == "pytest" and hashes:
            spec = dict(grader.spec)
            spec["fixture_hashes"] = hashes
            graders.append(GraderSpec("pytest", spec))
        else:
            graders.append(grader)
    clone = Task(**{**task.__dict__, "graders": graders})
    return clone


# --- execution ------------------------------------------------------------


@dataclass
class _BudgetWatch:
    """Poll the run's own session log so a runaway can be stopped mid-flight."""

    sessions_root: Path
    cwd: str
    started_at: float
    token_budget: int
    max_turns: int
    interval: float = TOKEN_POLL_SECONDS
    last_poll: float = 0.0
    reason: Optional[str] = None
    usage: dict = field(default_factory=dict)
    engine: dict = field(default_factory=dict)

    def poll(self, force: bool = False) -> Optional[str]:
        """Return a breach reason, or None while within budget.

        ``force`` performs the final poll once the agent has exited, so a fast
        run's usage is still recorded instead of racing the poll interval.
        """
        now = time.monotonic()
        if not force and now - self.last_poll < self.interval:
            return self.reason
        self.last_poll = now
        log = newest_session_log(self.sessions_root, cwd=self.cwd,
                                 started_after=self.started_at)
        if log is None:
            return self.reason
        try:
            transcript = parse_session(log)
        except Exception:
            return self.reason
        self.usage = transcript.usage.to_dict()
        self.engine = transcript.engine.to_dict()
        if self.token_budget and \
                self.usage["total_tokens"] > self.token_budget:
            self.reason = (f"token budget exceeded: "
                           f"{self.usage['total_tokens']} > "
                           f"{self.token_budget}")
        elif self.max_turns and self.engine["turns"] > self.max_turns:
            self.reason = (f"turn budget exceeded: {self.engine['turns']} > "
                           f"{self.max_turns}")
        return self.reason


def run_task(task: Task, suite_root: Path, system: System,
             config: Optional[RunnerConfig] = None,
             workspace: Optional[Path] = None) -> TaskOutcome:
    """Stage, execute, grade, and account for one task."""
    config = config or RunnerConfig()
    errors = system.validate()
    if errors:
        return TaskOutcome(task_id=task.task_id, system=system.name,
                           status=STS_ERROR, problems=errors)

    workspace = stage_workspace(suite_root, task, into=workspace)
    run_id = f"{task.task_id}-{int(time.time() * 1000)}"
    run_dir = config.resolved_state_dir() / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "agent.log"

    outcome = TaskOutcome(task_id=task.task_id, system=system.name,
                          status=STS_ERROR, run_id=run_id,
                          workspace=str(workspace), log_path=str(log_path))

    hashes = fixture_hashes(workspace, task)
    graded_task = _bind_fixture_hashes(task, hashes)

    try:
        prompt = _load_kickoff(suite_root, task)
        argv = build_argv(system, prompt, workspace)
    except (ValueError, RunnerError) as exc:
        outcome.problems.append(str(exc))
        _persist(outcome, run_dir)
        return outcome

    missing = _unresolvable_command(system, workspace)
    if missing:
        outcome.problems.append(missing)
        _persist(outcome, run_dir)
        return outcome

    watch = _BudgetWatch(
        sessions_root=config.resolved_sessions_root(),
        # Resolved, because a session log is keyed by the path the agent
        # reports, and a symlinked workspace would otherwise never match.
        cwd=str(Path(workspace).resolve()),
        started_at=time.time(),
        token_budget=task.budgets.token_budget,
        max_turns=task.budgets.max_turns,
        interval=config.token_poll_seconds,
    )

    started = time.monotonic()
    outcome.exit_code, breach = _spawn(argv, workspace, config, log_path,
                                       task.budgets.wall_seconds, watch)
    outcome.wall_seconds = time.monotonic() - started
    outcome.usage = watch.usage
    outcome.engine = watch.engine

    if breach == "timeout":
        outcome.status = STS_TIMEOUT
        outcome.problems.append(
            f"wall-clock budget exceeded: {task.budgets.wall_seconds}s")
    elif breach:
        outcome.status = STS_BREACHED
        outcome.problems.append(breach)
    else:
        grades = _grade(graded_task, workspace, suite_root)
        outcome.grades = grades
        outcome.status = STS_VERIFIED if overall(grades) else STS_FAILED

    _persist(outcome, run_dir)
    if not config.keep_workspace and outcome.status in (STS_VERIFIED,
                                                        STS_FAILED):
        shutil.rmtree(workspace, ignore_errors=True)
        outcome.workspace = None
    return outcome


def _unresolvable_command(system: System, workspace: Path) -> Optional[str]:
    """Why the command would not start, checked from the workspace it runs in.

    A path-like command is resolved relative to the spawn cwd, because that is
    what the OS will do. Checking it from the runner's own cwd would pass a
    command that then fails to start.
    """
    program = system.command[0]
    if os.sep in program:
        candidate = Path(program)
        if not candidate.is_absolute():
            candidate = (workspace / candidate)
        if not candidate.is_file():
            return (f"command not found at {candidate} (a path-like command "
                    "is resolved against the task workspace)")
        return None
    if resolved_binary(system) is None:
        return f"command not found on PATH: {program!r}"
    return None


def _grade(task: Task, workspace: Path, suite_root) -> list[GradeResult]:
    from cupola.graders import grade

    context = GradeContext(workspace=workspace, suite_root=suite_root)
    return grade(task.graders, context)


def _load_kickoff(suite_root: Path, task: Task) -> str:
    if not task.kickoff:
        raise RunnerError(f"task {task.task_id} has no kickoff prompt")
    path = suite_root / task.kickoff
    if not path.is_file():
        raise RunnerError(f"kickoff file missing: {path}")
    return path.read_text()


def _spawn(argv: list[str], workspace: Path, config: RunnerConfig,
           log_path: Path, wall_seconds: int,
           watch: _BudgetWatch) -> tuple[Optional[int], Optional[str]]:
    """Run the agent with a wall-clock deadline and a token/turn watcher."""
    env = dict(os.environ)
    env.update(config.child_env())
    deadline = time.monotonic() + wall_seconds
    breach: Optional[str] = None

    with open(log_path, "wb") as log:
        try:
            proc = subprocess.Popen(argv, cwd=str(workspace), env=env,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        except OSError as exc:
            return None, f"failed to start agent: {exc}"

        while True:
            code = proc.poll()
            if code is not None:
                watch.poll(force=True)
                return code, breach
            if time.monotonic() >= deadline:
                breach = "timeout"
                _kill(proc)
                return proc.poll(), breach
            reason = watch.poll()
            if reason:
                breach = reason
                _kill(proc)
                return proc.poll(), breach
            time.sleep(0.5)


def _kill(proc: subprocess.Popen) -> None:
    """Kill the whole process group: an agent may have children."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def _persist(outcome: TaskOutcome, run_dir: Path) -> None:
    """Write the outcome immediately; a crash must not erase evidence."""
    try:
        (run_dir / "outcome.json").write_text(
            json.dumps(outcome.to_dict(), indent=2) + "\n")
    except OSError:
        pass


def run_suite(suite_id: str, system: System, benchmark_root: Optional[Path] = None,
              task_id: Optional[str] = None, runs: int = 1,
              config: Optional[RunnerConfig] = None) -> list[TaskOutcome]:
    """Run selected tasks, repeatedly. Returns outcomes in execution order."""
    suite = load_suite(suite_id, benchmark_root)
    if suite.errors:
        raise RunnerError("suite is invalid: " + "; ".join(suite.errors))
    if suite.root is None:
        raise RunnerError(f"suite {suite_id} has no root directory")

    tasks = [suite.task(task_id)] if task_id else list(suite.tasks)
    if task_id and tasks[0] is None:
        raise RunnerError(f"suite {suite_id} has no task {task_id!r}")

    outcomes: list[TaskOutcome] = []
    for task in tasks:
        for _ in range(max(1, runs)):
            outcomes.append(run_task(task, suite.root, system, config))
    return outcomes


def tail_log(path: Path, limit: int = OUTPUT_TAIL_BYTES) -> str:
    """Last part of a run log, for the report and for debugging."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


def measure_direct(system: System, timeout: int = 60) -> dict:
    """Time a bare ``http`` system with one tiny completion.

    Used for the model-only baseline: no agent scaffold, no tools, just the
    endpoint. Kept here rather than in the harness module because it is an
    execution concern.
    """
    if system.transport != "http":
        raise RunnerError("measure_direct needs an http system")
    import urllib.error
    import urllib.request

    payload = json.dumps({
        "model": system.model or "local",
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 8,
        "stream": False,
    }).encode()
    request = urllib.request.Request(
        f"{(system.base_url or '').rstrip('/')}/chat/completions",
        data=payload, headers={"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
        elapsed = time.monotonic() - started
        usage = body.get("usage") or {}
        return {
            "ok": True,
            "wall_seconds": round(elapsed, 3),
            "output_tokens": usage.get("completion_tokens"),
            "input_tokens": usage.get("prompt_tokens"),
            "text": (body.get("choices") or [{}])[0]
                    .get("message", {}).get("content", ""),
        }
    except (urllib.error.URLError, ValueError, KeyError, TimeoutError) as exc:
        return {"ok": False, "error": str(exc),
                "wall_seconds": round(time.monotonic() - started, 3)}


def preflight(system: System, health_url: Optional[str] = None) -> list[str]:
    """Refuse to start a run against a system that cannot answer."""
    problems: list[str] = []
    if system.transport == "http" and system.base_url:
        import urllib.error
        import urllib.request

        url = (health_url or system.base_url.rstrip("/") + "/models")
        try:
            with urllib.request.urlopen(url, timeout=5):
                pass
        except (urllib.error.URLError, OSError) as exc:
            problems.append(f"endpoint not reachable at {url}: {exc}")
    elif system.transport == "process":
        program = system.command[0] if system.command else ""
        if not (os.sep in program or resolved_binary(system)):
            problems.append(
                f"command not found on PATH: {program!r}; a path-like command "
                "is checked against the task workspace at run time")
    return problems
