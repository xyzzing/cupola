"""Named systems under test.

A benchmark run needs to say *what* it measured. Systems are declared in
``benchmarks/systems.json`` rather than assembled on the command line, so a
run's report can name exactly what it ran and a reader can reproduce it.

Declaring systems in a file also keeps the A/B honest: two arms differ by a
declared field, not by whatever flags happened to be typed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from cupola.harness import TRANSPORT_HTTP, TRANSPORT_PROCESS, System, system_from_dict
from cupola.suites import benchmarks_root

SYSTEMS_FILENAME = "systems.json"

#: Lane profile recorded when a system declares none. Overridable so a run on
#: another machine, or in a test, records the lanes that actually applied.
DEFAULT_LANE_PROFILE = os.environ.get("CUPOLA_LANE_PROFILE", "web")


class SystemError(Exception):
    """Systems cannot be loaded (missing file, bad JSON, unknown name)."""


def systems_path(root: Optional[Path] = None) -> Path:
    env = os.environ.get("CUPOLA_SYSTEMS_FILE")
    if env:
        return Path(env)
    return benchmarks_root(root) / SYSTEMS_FILENAME


def load_systems(root: Optional[Path] = None) -> list[System]:
    """Every declared system. An absent file is not an error."""
    path = systems_path(root)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SystemError(f"cannot read {path}: {exc}") from exc
    entries = raw.get("systems") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise SystemError(f"{path} must contain a list of systems")
    systems = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SystemError(f"{path} has a non-object system entry")
        system = system_from_dict(entry)
        # Manifest-relative paths are repo-relative: systems.json lives in
        # benchmarks/, and its commands point at repo files.
        _anchor_command(system, path.parent.parent)
        systems.append(system)
    return systems


def find_system(name: str, root: Optional[Path] = None) -> System:
    """Look up one declared system by name."""
    systems = load_systems(root)
    for system in systems:
        if system.name == name:
            return system
    known = ", ".join(s.name for s in systems) or "none declared"
    raise SystemError(f"unknown system {name!r} (declared: {known})")


def _anchor_command(system, base: Path) -> None:
    """Anchor relative script paths in a system's argv against the repo root.

    An agent runs with the task workspace as its cwd, so a relative script
    path would resolve against the workspace and silently fail to start. Only
    *file-like* tokens are rewritten: a bare program name stays as-is so PATH
    lookup still works, and ``{...}`` placeholders and flags are left alone.
    """
    if system.transport != TRANSPORT_PROCESS or not system.command:
        return
    anchored = []
    for index, token in enumerate(system.command):
        anchored.append(_anchor_token(token, base, is_program=index == 0))
    system.command = anchored


def _anchor_token(token: str, base: Path, is_program: bool) -> str:
    if not token or token.startswith("-") or token.startswith("{"):
        return token
    if os.sep not in token:
        return token
    candidate = Path(token)
    if candidate.is_absolute():
        return token
    if is_program:
        return str((base / candidate).resolve())
    # A path-like argument: anchor it only when the file actually exists at the
    # repo root, so a legitimate workspace-relative argument is not rewritten.
    resolved = (base / candidate).resolve()
    return str(resolved) if resolved.exists() else token


def lane_profile_for(system: System) -> str:
    """Which dsh profile's lanes a run of this system should record."""
    return system.profile or DEFAULT_LANE_PROFILE


def default_systems() -> list[System]:
    """What ``benchmarks/systems.json`` ships with.

    The stub exists so the harness is testable end to end with no model and no
    dsh; the live arms are the ponytail A/B that M5 wires up. The
    ``local-http`` arm is the model-only baseline: no agent scaffold at all.
    """
    repo_root = Path(__file__).resolve().parents[2]
    return [
        System(
            name="stub-agent",
            transport=TRANSPORT_PROCESS,
            command=["python3", str(repo_root / "tests" / "fixtures"
                                    / "stub_agent.py"), "{prompt}"],
            notes="no model, no network: exercises the harness itself",
        ),
        System(
            name="local-http",
            transport=TRANSPORT_HTTP,
            base_url="http://127.0.0.1:8080/v1",
            model="local",
            model_route="local",
            notes="model-only baseline over the running llama-server",
        ),
    ]
