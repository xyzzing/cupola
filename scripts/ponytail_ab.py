#!/usr/bin/env python3
"""Set up and run the ponytail before/after benchmark on this machine.

This script exists because the setup step needs write access to ``$DSH_HOME``,
which the sandbox denies to an agent. Run it yourself:

    python3 scripts/ponytail_ab.py setup     # create the bench profile + arms
    python3 scripts/ponytail_ab.py baseline  # run the off-arm
    python3 scripts/ponytail_ab.py candidate # run the on-arm
    python3 scripts/ponytail_ab.py compare   # attribute the difference

What it builds
--------------

A ``bench`` dsh profile, copied from ``headless`` (which already carries the
minder bridge and the task-budget gate) plus:

* the **local provider block** that currently lives only in ``web`` — without
  it ``--profile headless`` has no route to the running llama-server;
* the **ponytail bridge loader**, given an ``id`` so a ``--patch`` overlay can
  target it. The live entry has no ``id``, and dsh patch targeting requires
  one: an overlay against it prints
  ``patch: entry "ponytail-hooks-bridge" not found``.

Two arms then differ by exactly one overlay file (``profiles/ponytail-off.yml``,
``- id: ponytail-hooks-bridge`` / ``disabled: true``), which is what makes the
lane-set delta the declared variable and nothing else.

Two independent tiers
---------------------

Ponytail ships twice on this machine and both must be toggled or the A/B is
half-blind: the hook tier above, and an always-on ``~/.dsh/AGENTS.md`` ruleset
block. ``setup`` reports which of the two is currently active.

Honest limits
-------------

Ponytail's hooks only ever emit ``systemMessage``/``additionalContext``; they
never deny or block a tool call. So this measures **context on/off**, not
enforcement on/off. dsh reads hook config once at host start, so restart the
host between arms.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BENCH_PROFILE = "bench"
SOURCE_PROFILE = "headless"
PONYTAIL_ENTRY_ID = "ponytail-hooks-bridge"
PONYTAIL_SHARE = Path.home() / ".local" / "share" / "ponytail"
PONYTAIL_HOOKS = PONYTAIL_SHARE / "hooks" / "dsh-hooks.json"
PONYTAIL_SKILL = PONYTAIL_SHARE / "skills" / "ponytail" / "SKILL.md"
PROVIDER_KEY = "local:"


def dsh_home() -> Path:
    import os
    return Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")


def profile_dir(name: str) -> Path:
    return dsh_home() / "profiles" / name


def _require_writable(path: Path) -> None:
    probe = path / ".ponytail-ab-write-probe"
    try:
        probe.write_text("x")
        probe.unlink()
    except OSError as exc:
        sys.exit(
            f"error: {path} is not writable ({exc}).\n"
            "This script edits your dsh profiles, so it cannot run under a\n"
            "write-restricted sandbox. Run it from an unrestricted shell.")


def _load_patch(path: Path) -> str:
    return path.read_text() if path.is_file() else "[]\n"


def _extract_local_provider(web_patch: Path) -> str | None:
    """Copy the `local:` provider block out of the live web profile.

    The block names a private model and a loopback endpoint, so it is read
    from the profile that already has it rather than hardcoded here.
    """
    if not web_patch.is_file():
        return None
    lines = web_patch.read_text().splitlines()
    start = None
    indent = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("local:"):
            start = index
            indent = len(line) - len(line.lstrip())
            break
    if start is None:
        return None
    block = []
    for line in lines[start:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent \
                and block:
            break
        block.append(line)
    return "\n".join(block).rstrip()


def setup(_args) -> int:
    source = profile_dir(SOURCE_PROFILE)
    target = profile_dir(BENCH_PROFILE)
    if not source.is_dir():
        sys.exit(f"error: no source profile at {source}")

    _require_writable(dsh_home() / "profiles")
    if target.exists():
        print(f"note: {target} already exists; updating in place")

    target.mkdir(parents=True, exist_ok=True)
    for name in ("cordis.yml", "cordis.patch.yml", "package.json",
                 "pnpm-workspace.yaml"):
        src = source / name
        if src.is_file() and not (target / name).exists():
            shutil.copy2(src, target / name)
    for name in ("node_modules", "pnpm-lock.yaml"):
        src, dst = source / name, target / name
        if src.exists() and not dst.exists():
            dst.symlink_to(src.resolve())
            print(f"  linked {name} -> {src}")

    # Copy the headless patch only when the bench profile has none yet, so a
    # second `setup` does not duplicate rows.
    patch = target / "cordis.patch.yml"
    text = _load_patch(patch)
    if "minder-bridge-loader" not in text:
        shutil.copy2(source / "cordis.patch.yml", patch)
        (target / "minder-bridge-loader.mjs").write_text(
            (source / "minder-bridge-loader.mjs").read_text())
        text = _load_patch(patch)

    if PROVIDER_KEY not in text:
        provider = _extract_local_provider(profile_dir("web") / "cordis.patch.yml")
        if provider is None:
            print("warning: no local provider found in the web profile; the "
                  "bench profile will have no model route")
        else:
            shifted = "\n".join(
                ("  " + line) if line.strip() else line
                for line in provider.splitlines())
            text += ("\n# Local provider, copied from the web profile so this\n"
                     "# headless-derived profile can reach the running server.\n"
                     "- id: llm-pi-ai\n  config:\n    providers:\n"
                     + shifted + "\n")
            print("  copied the local provider block from the web profile")
    if PONYTAIL_ENTRY_ID not in text:
        if not PONYTAIL_HOOKS.is_file():
            print(f"warning: {PONYTAIL_HOOKS} is missing; the on-arm will "
                  "declare a lane that cannot activate")
        text += PONYTAIL_BLOCK
        (target / "ponytail-bridge-loader.mjs").write_text(LOADER_SOURCE)
        print("  added the ponytail loader entry (with an id, so --patch can "
              "target it)")
    patch.write_text(text)

    arms_dir = REPO / "profiles"
    arms_dir.mkdir(exist_ok=True)
    (arms_dir / "ponytail-off.yml").write_text(
        f"# One declared difference between the two arms.\n"
        f"- id: {PONYTAIL_ENTRY_ID}\n"
        f"  disabled: true\n")
    print(f"  wrote {arms_dir / 'ponytail-off.yml'}")

    print("\nActive tiers right now:")
    hook_state = (f"declared in {BENCH_PROFILE}"
                  if PONYTAIL_ENTRY_ID in text else "absent")
    print(f"  hook tier     : {hook_state}")
    agents = dsh_home() / "AGENTS.md"
    if agents.is_file():
        body = agents.read_text()
        state = "ruleset block present" if "lazy senior developer" in body \
            else "pointer only (hook tier injects the rules)"
        print(f"  AGENTS.md tier: {state} ({agents})")
    print(f"\nNext: python3 {Path(__file__).name} baseline")
    return 0


def _run_arm(system: str, out: Path, runs: int, task: str | None) -> int:
    import os
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(REPO / "src"))
    argv = [sys.executable, "-m", "cupola.cli", "bench", "suite", "run",
            "--suite", "coding-core-v2", "--system", system, "--execute",
            "--runs", str(runs), "--out", str(out)]
    if task:
        argv += ["--task", task]
    print("running:", " ".join(argv))
    print("NOTE: restart the dsh host between arms; hook config is read once "
          "at host start.")
    return subprocess.call(argv, env=env, cwd=str(REPO))


def baseline(args) -> int:
    return _run_arm("local-ponytail-off", REPO / "reports" / "ponytail-off.json",
                    args.runs, args.task)


def candidate(args) -> int:
    return _run_arm("local-ponytail-on", REPO / "reports" / "ponytail-on.json",
                    args.runs, args.task)


def compare(_args) -> int:
    baseline_report = REPO / "reports" / "ponytail-off.json"
    candidate_report = REPO / "reports" / "ponytail-on.json"
    for path in (baseline_report, candidate_report):
        if not path.is_file():
            sys.exit(f"error: missing {path}; run baseline and candidate first")
    argv = [sys.executable, "-m", "cupola.cli", "bench", "compare",
            "--baseline", str(baseline_report),
            "--candidate", str(candidate_report),
            "--attribute", "marginal",
            "--arm", f"ponytail={candidate_report}"]
    import os
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(REPO / "src"))
    return subprocess.call(argv, env=env, cwd=str(REPO))


PONYTAIL_BLOCK = f"""
# ponytail hook tier. The id is required for --patch overlays to target this
# row; the live web-profile entry has none, so it cannot be toggled.
- insert:
    - id: {PONYTAIL_ENTRY_ID}
      name: ./ponytail-bridge-loader.mjs
      config:
        configPath: {PONYTAIL_HOOKS}
        pluginRoot: {PONYTAIL_SHARE}
        defaultTimeoutMs: 5000
"""

LOADER_SOURCE = '''// Re-exports the Claude-format hooks bridge, so the ponytail hook config
// mounts as a file entry (package entries with an `inject` export do not).
import * as bridge from "@deepseek-ai/dsh-hooks-claude-code";
export const inject = bridge.inject;
export const Config = bridge.Config;
export const name = "ponytail-hooks-bridge";
export function apply(ctx, config) {
  return bridge.apply(ctx, config);
}
'''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("setup", help="create the bench profile and A/B arms")
    for name in ("baseline", "candidate"):
        sub = subs.add_parser(name, help=f"run the {name} arm")
        sub.add_argument("--runs", type=int, default=3,
                         help="independent runs per task (default 3)")
        sub.add_argument("--task", help="only this task")
    subs.add_parser("compare", help="attribute the difference")

    args = parser.parse_args(argv)
    return {"setup": setup, "baseline": baseline, "candidate": candidate,
            "compare": compare}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
