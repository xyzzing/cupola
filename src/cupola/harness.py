"""What is under test: an agent harness plus a model route.

A benchmark compares *systems*, not models. The same model reached through
``dsh --profile bench`` with different lane sets, or through a different
scaffold, or over a bare OpenAI-compatible endpoint, are different systems and
must be labelled as such. That is why a :class:`System` carries the transport,
the command, the route, the thinking effort and the dsh profile.

Two transports exist:

``process``
    Spawn an agent CLI. The command is an argv list, never a shell string:
    a benchmark that pipes user-authored prompts through ``shell=True`` is a
    command-injection hole and hides which binary actually ran.

``http``
    Talk to an OpenAI-compatible endpoint directly, for measuring a model
    without any agent scaffold around it.

Nothing here executes anything; the runner does that. This module only builds
argv and validates that a system is usable, so it is testable offline.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

TRANSPORT_PROCESS = "process"
TRANSPORT_HTTP = "http"
TRANSPORTS = (TRANSPORT_PROCESS, TRANSPORT_HTTP)

#: Placeholders a command template may use.
PROMPT_PLACEHOLDER = "{prompt}"
WORKSPACE_PLACEHOLDER = "{workspace}"
PROFILE_PLACEHOLDER = "{profile}"
MODEL_PLACEHOLDER = "{model}"

_PLACEHOLDERS = (PROMPT_PLACEHOLDER, WORKSPACE_PLACEHOLDER,
                 PROFILE_PLACEHOLDER, MODEL_PLACEHOLDER)


@dataclass
class System:
    """A named system under test."""

    name: str
    transport: str = TRANSPORT_PROCESS
    command: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    profile: Optional[str] = None
    model: Optional[str] = None
    model_route: Optional[str] = None
    thinking_effort: Optional[str] = None
    sandbox: Optional[str] = None
    base_url: Optional[str] = None
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "transport": self.transport,
            "command": list(self.command),
            "profile": self.profile,
            "model": self.model,
            "model_route": self.model_route,
            "thinking_effort": self.thinking_effort,
            "sandbox": self.sandbox,
            "base_url": self.base_url,
            "env_keys": sorted(self.env),
            "notes": self.notes,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.name:
            errors.append("system name is required")
        if self.transport not in TRANSPORTS:
            errors.append(f"transport must be one of {TRANSPORTS}, "
                          f"got {self.transport!r}")
        if self.transport == TRANSPORT_PROCESS:
            if not self.command:
                errors.append("a process system needs a command")
            else:
                missing = [token for token in self.command
                           if any(placeholder in token
                                  for placeholder in _PLACEHOLDERS)]
                if not missing:
                    errors.append(
                        "a process command must reference the task prompt, "
                        f"e.g. {PROMPT_PLACEHOLDER}")
        elif self.transport == TRANSPORT_HTTP:
            if not self.base_url:
                errors.append("an http system needs a base_url")
        return errors


def build_argv(system: System, prompt: str, workspace: Path) -> list[str]:
    """Resolve a system's command template into argv (never a shell string)."""
    errors = system.validate()
    if errors:
        raise ValueError("invalid system: " + "; ".join(errors))
    if system.transport != TRANSPORT_PROCESS:
        raise ValueError(f"transport {system.transport!r} has no argv")

    substitutions = {
        PROMPT_PLACEHOLDER: prompt,
        WORKSPACE_PLACEHOLDER: str(workspace),
        PROFILE_PLACEHOLDER: system.profile or "",
        MODEL_PLACEHOLDER: system.model or "",
    }
    argv: list[str] = []
    for token in system.command:
        resolved = token
        for placeholder, value in substitutions.items():
            resolved = resolved.replace(placeholder, value)
        argv.append(resolved)
    return argv


def resolved_binary(system: System) -> Optional[str]:
    """Absolute path of the program the system would run, if resolvable."""
    if system.transport != TRANSPORT_PROCESS or not system.command:
        return None
    return shutil.which(system.command[0])


def system_from_dict(raw: dict) -> System:
    """Build a System from a manifest/systems-file entry."""
    return System(
        name=str(raw.get("name") or ""),
        transport=str(raw.get("transport") or TRANSPORT_PROCESS),
        command=[str(t) for t in raw.get("command") or []],
        env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
        profile=raw.get("profile"),
        model=raw.get("model"),
        model_route=raw.get("model_route"),
        thinking_effort=raw.get("thinking_effort"),
        sandbox=raw.get("sandbox"),
        base_url=raw.get("base_url"),
        notes=str(raw.get("notes") or ""),
    )


#: The two arms of the ponytail A/B, expressed as one overlay difference.
#: Built here so the pair is described in one place; the overlay files
#: themselves are written by the bench profile setup.
PONYTAIL_ON = "local-ponytail-on"
PONYTAIL_OFF = "local-ponytail-off"


def ponytail_arm(name: str, dsh_bin: str = "dsh", profile: str = "bench",
                 overlay: Optional[Path] = None) -> System:
    """One arm of the ponytail A/B.

    The arms differ only by ``--patch <overlay>``, so the lane-set difference
    is exactly the declared variable. The kickoff prompt is one argv token, so
    it reaches the harness verbatim without a shell in between.
    """
    command = [dsh_bin, "--profile", profile]
    if name == PONYTAIL_OFF and overlay is not None:
        command += ["--patch", str(overlay)]
    command.append(PROMPT_PLACEHOLDER)
    return System(
        name=name,
        transport=TRANSPORT_PROCESS,
        command=command,
        profile=profile,
        notes=("ponytail hook tier disabled via overlay"
               if name == PONYTAIL_OFF else "ponytail hook tier enabled"),
    )
