"""Toolchain lane inventory (Compass prerequisite).

A "lane" is one independently toggleable enhancement to the agent toolchain:
a bundle, a package dependency, a patch-layer entry, a skill, an always-on
ruleset file, or an MCP server. Plugins accumulate over time, so the toolchain
is tracked as an inventory of named lanes rather than one opaque hash.

Two rules keep this honest:

* Discovery is generic. Nothing here is a hand-maintained plugin registry —
  it is recomputed from the profile's ``package.json``, the composed loader
  tree from ``dsh --dump-config``, the skill directories and the ruleset
  files. A registry would drift; a scan cannot.
* Declared is not observed. ``dsh`` hooks fail open and silently, so a lane
  can be active in the composed tree and still do nothing at runtime. The
  composed tree gives ``declared``; a run's gates give ``observed``. Only a
  lane that passed its gate may be credited with an effect.

``dsh --dump-config`` composes bundle layers, then the profile patch, then the
home patch, then ``--patch`` overlays, so it is the single source of truth for
patch precedence. It also rewrites ``cordis.yml`` in the profile directory,
which fails with EROFS under a write-restricted sandbox — callers must run
unconfined or against a copy of ``DSH_HOME``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

SCHEMA_VERSION = 1

# Lane kinds — the closed vocabulary an inventory may contain.
KIND_BUNDLE = "bundle"
KIND_DEPENDENCY = "dependency"
KIND_PATCH_ENTRY = "patch-entry"
KIND_SKILL = "skill"
KIND_RULESET = "ruleset"
KIND_MCP = "mcp"
LANE_KINDS = (KIND_BUNDLE, KIND_DEPENDENCY, KIND_PATCH_ENTRY, KIND_SKILL,
              KIND_RULESET, KIND_MCP)

# Declaration states observed in the composed tree.
DECLARED_ACTIVE = "active"
DECLARED_DISABLED = "disabled"
DECLARED_MISSING = "missing"      # declared by a layer, resolves to nothing

# Run-time observation states.
OBSERVED_UNKNOWN = "unknown"      # no gate declared (expected between runs)
OBSERVED_OK = "ok"
OBSERVED_NO_OP = "no_op"          # gate failed: active but provably inert
OBSERVED_FAILED = "failed"

# Where a lane was decided. Later layers win.
LAYER_BUNDLE = "bundle"
LAYER_PROFILE_PATCH = "profile-patch"
LAYER_HOME_PATCH = "home-patch"
LAYER_RUN_PATCH = "run-patch"
LAYER_INVENTORY = "inventory"

DEFAULT_DUMP_TIMEOUT = 120

_DEAD_REF_RE = re.compile(
    r"\[(?P<patch>[^\]]+)\]\s+patch:\s+entry\s+\"(?P<entry>[^\"]+)\"\s+not found")
_SKIP_BUNDLE_RE = re.compile(
    r"skipping profile bundle \"(?P<bundle>[^\"]+)\":\s*(?P<reason>.*)")
_ENTRY_OPEN_RE = re.compile(r"^- ([A-Za-z_][\w-]*):(?: (.*))?$")
#: An item of a nested YAML sequence, e.g. ``- '-y'`` under ``args:``.
_LIST_ITEM_RE = re.compile(r"^\s*- (?!\s*[A-Za-z_][\w-]*:)")
_HOME_KEY_RE = re.compile(r"^( *)([A-Za-z_][\w-]*):(?: (.*))?$")
_JS_EXPR_RE = re.compile(r"^!!js\b")


class LaneError(Exception):
    """Lane discovery failed in a way the operator must fix."""


@dataclass
class Effects:
    """What a lane mechanically offers, derived rather than self-declared."""

    hook_events: list[str] = field(default_factory=list)
    injected_context_sha256: Optional[str] = None
    injected_context_chars: Optional[int] = None
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    config_sha256: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "hook_events": list(self.hook_events),
            "injected_context_sha256": self.injected_context_sha256,
            "injected_context_chars": self.injected_context_chars,
            "skills": list(self.skills),
            "mcp_servers": list(self.mcp_servers),
            "config_sha256": self.config_sha256,
        }


@dataclass
class Observation:
    """Run-time evidence that a lane actually did something."""

    status: str = OBSERVED_UNKNOWN
    observed: dict = field(default_factory=dict)
    detail: str = ""
    at: Optional[float] = None

    def to_dict(self) -> dict:
        return {"status": self.status, "observed": self.observed,
                "detail": self.detail, "at": self.at}


@dataclass
class Lane:
    """One trackable enhancement to the agent toolchain."""

    lane_id: str
    kind: str
    source: str = ""
    declared: str = DECLARED_ACTIVE
    source_hash: Optional[str] = None
    layer: str = LAYER_INVENTORY
    enabled_by: Optional[str] = None
    detail: str = ""
    effects: Effects = field(default_factory=Effects)
    observation: Observation = field(default_factory=Observation)

    def to_dict(self) -> dict:
        return {
            "lane_id": self.lane_id,
            "kind": self.kind,
            "source": self.source,
            "declared": self.declared,
            "source_hash": self.source_hash,
            "layer": self.layer,
            "enabled_by": self.enabled_by,
            "detail": self.detail,
            "effects": self.effects.to_dict(),
            "observation": self.observation.to_dict(),
        }


@dataclass
class DeadReference:
    """A patch layer targeting an entry that no longer exists."""

    patch: str
    entry: str

    def to_dict(self) -> dict:
        return {"patch": self.patch, "entry": self.entry}


@dataclass
class LaneSet:
    """A full, hashable inventory of the toolchain at one moment."""

    schema_version: int = SCHEMA_VERSION
    generated_at: float = field(default_factory=time.time)
    dsh_home: str = ""
    profile: str = ""
    lanes: list[Lane] = field(default_factory=list)
    dead_references: list[DeadReference] = field(default_factory=list)
    unresolved_bundles: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "dsh_home": self.dsh_home,
            "profile": self.profile,
            "lane_set_hash": self.lane_set_hash(),
            "lanes": [lane.to_dict() for lane in self.lanes],
            "dead_references": [ref.to_dict() for ref in self.dead_references],
            "unresolved_bundles": list(self.unresolved_bundles),
            "errors": list(self.errors),
        }

    def lane_set_hash(self) -> str:
        """Hash of what is installed and enabled — not of what was observed."""
        core = [
            {
                "lane_id": lane.lane_id,
                "kind": lane.kind,
                "declared": lane.declared,
                "source_hash": lane.source_hash,
                "effects": lane.effects.to_dict(),
            }
            for lane in sorted(self.lanes, key=lambda item: item.lane_id)
        ]
        return _sha256_text(_canonical(core))

    def by_id(self) -> dict[str, Lane]:
        return {lane.lane_id: lane for lane in self.lanes}

    def active(self) -> list[Lane]:
        return [lane for lane in self.lanes
                if lane.declared == DECLARED_ACTIVE]

    def warnings(self) -> list[str]:
        """Operator-visible problems that are not fatal."""
        problems = [f"unresolved bundle: {name}"
                    for name in self.unresolved_bundles]
        problems += [f"dead patch reference: {ref.patch} -> {ref.entry}"
                     for ref in self.dead_references]
        problems += [f"lane did not activate: {lane.lane_id}"
                     for lane in self.lanes
                     if lane.observation.status == OBSERVED_NO_OP]
        return problems


# --- hashing helpers ------------------------------------------------------


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    """SHA-256 over a file's bytes, or over a marker for unreadable paths."""
    sha = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                sha.update(chunk)
    except OSError as exc:
        return f"unreadable:{exc.__class__.__name__}"
    return sha.hexdigest()


#: Directories skipped when fingerprinting a linked source tree. Without this,
#: a link lane would hash dependency caches and produce a hash that changes
#: when anything else on the machine does.
_TREE_SKIP_DIRS = frozenset((
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".pytest_cache", ".ruff_cache", ".mypy_cache",
))
_TREE_MAX_FILES = 2000


def sha256_tree(root: Path, max_files: int = _TREE_MAX_FILES) -> str:
    """Deterministic fingerprint of a source tree.

    Uses the git commit when the tree is a clean-ish checkout, because that is
    both cheaper and more meaningful than hashing build output. Falls back to
    a sorted path+content hash over source-like files.
    """
    root = Path(root)
    if not root.exists():
        return "missing"
    git_head = _git_head(root)
    if git_head:
        return f"git:{git_head}"

    entries: list[tuple[str, str]] = []
    count = 0
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if _TREE_SKIP_DIRS.intersection(path.parts):
            continue
        if not path.is_file():
            continue
        count += 1
        if count > max_files:
            return f"tree-over-limit:{len(root.parts)}:{max_files}"
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        entries.append((rel, sha256_file(path)))
    return _sha256_text(_canonical(entries))


def _git_head(root: Path) -> Optional[str]:
    git_dir = root / ".git"
    if not (git_dir.exists() or git_dir.is_file()):
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


# --- composed-tree parsing ------------------------------------------------

#: Parsed scalars stay as raw strings. Config values are only ever hashed or
#: compared, never interpreted, so ``!!js`` expressions and flow collections
#: can be left verbatim.


def _parse_scalar(raw: str):
    text = raw.strip()
    if not text:
        return ""
    if text.startswith(("!!js", "!!str")):
        return text
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
    if text in ("null", "~"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _fold_block(lines: list[str], start: int, indent: int,
                indicator: str = "|") -> tuple[object, int]:
    """Collect a block scalar body and apply its folding rule.

    ``>`` folds newlines into spaces; ``|`` keeps them. The composed tree uses
    folded form for entry names (``name: >-``), which is how a long
    ``file://`` path is emitted.
    """
    body: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            body.append("")
            index += 1
            continue
        current = len(line) - len(line.lstrip())
        if current <= indent:
            break
        body.append(line.strip())
        index += 1
    while body and not body[-1]:
        body.pop()
    if indicator.startswith(">"):
        return " ".join(part for part in body if part), index
    return "\n".join(body), index


#: Block-scalar indicators, so a folded ``name: >-`` is not mistaken for a
#: plain scalar (the file-entry loaders in a composed tree use this form).
_BLOCK_INDICATORS = ("|", "|-", "|+", ">", ">-", ">+")


def _parse_yaml_lite(lines: list[str]) -> list[dict]:
    """Parse the ``dsh --dump-config`` document into entry dicts.

    Handles the subset that document actually uses: a top-level sequence of
    mappings, nested mappings by indentation, scalars, block scalars, and
    flow collections kept verbatim. It is deliberately not a general YAML
    parser — adding a YAML dependency for one document format is not a trade
    this project makes.
    """
    entries: list[dict] = []
    current: Optional[dict] = None
    stack: list[tuple[int, dict]] = []

    index = 0
    while index < len(lines):
        raw = lines[index]
        index += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue

        if raw.startswith("- "):
            current = {}
            entries.append(current)
            stack = [(-1, current)]
            # Visual column of the key: the "- " marker plus any indent the
            # sequence itself carries. A folded value's body must be more
            # indented than this, or it would swallow the entry's own keys.
            key_column = 2 + (len(raw) - len(raw.lstrip()))
            match = _HOME_KEY_RE.match(" " + raw[2:])
            if match:
                key, raw_value = match.group(2), (match.group(3) or "").strip()
                if raw_value in _BLOCK_INDICATORS:
                    block, index = _fold_block(lines, index, key_column,
                                               raw_value)
                    current[key] = block
                else:
                    current[key] = _parse_scalar(raw_value)
            continue

        if current is None:
            continue

        match = _HOME_KEY_RE.match(raw)
        if not match:
            # A nested sequence item ("- '-y'") or other construct this subset
            # does not model. Keep the text so the lane's config hash still
            # changes when it does; nothing here is interpreted.
            if stack:
                stack[-1][1].setdefault("_items", []).append(raw.strip())
            continue
        indent = len(match.group(1))
        key, raw_value = match.group(2), (match.group(3) or "").strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1] if stack else current

        if raw_value in _BLOCK_INDICATORS:
            block, index = _fold_block(lines, index, indent, raw_value)
            parent[key] = block
            continue
        if not raw_value:
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
            continue
        parent[key] = _parse_scalar(raw_value)

    return entries


def _layer_comment(line: str) -> Optional[tuple[str, Optional[str]]]:
    """Parse a ``# ==`` banner into (layer, patch).

    Two forms occur: ``# == <bundle>, patched by <patch>`` and, for entries a
    patch added outright, ``# == <patch-path>``.
    """
    if not line.startswith("# =="):
        return None
    body = line[4:].strip()
    if ", patched by " in body:
        layer, patched = body.split(", patched by ", 1)
        return layer.strip(), patched.strip()
    if body.endswith((".yml", ".yaml")):
        return body, body
    return body, None


def _is_block_line(line: str) -> bool:
    """True when this entry-opening line opens a block scalar (``name: >-``)."""
    body = line[2:] if line.startswith("- ") else line
    match = _HOME_KEY_RE.match(" " + body)
    return bool(match and (match.group(3) or "").strip() in _BLOCK_INDICATORS)


def parse_dump(text: str) -> list[dict]:
    """Parse composed-tree text into entries carrying their deciding layer.

    Entries are sliced on top-level ``- `` lines first, then each slice is
    parsed on its own. That keeps layer attribution exact instead of relying
    on entry-order alignment between two parses.
    """
    entries: list[dict] = []
    pending_layer: Optional[str] = None
    pending_patch: Optional[str] = None
    group: list[str] = []
    in_block = False
    entry_indent = 0

    def flush() -> None:
        if not group:
            return
        parsed = _parse_yaml_lite(group)
        entry = parsed[0] if parsed else {}
        entry["_layer"] = pending_layer
        entry["_patch"] = pending_patch
        entries.append(entry)

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if group:
                group.append(line)
            continue

        comment = _layer_comment(line)
        if comment and not in_block:
            flush()
            group = []
            pending_layer, pending_patch = comment
            continue

        indent = len(line) - len(line.lstrip())
        if group:
            if indent > entry_indent or (in_block and indent > entry_indent):
                group.append(line)
                if not in_block and _is_block_line(line) and \
                        not _LIST_ITEM_RE.match(line):
                    in_block = True
                continue
            # A new entry may start at the same indent as the previous one.
            if indent == entry_indent and line.startswith("- ") and \
                    not _LIST_ITEM_RE.match(line):
                flush()
                group = [line]
                entry_indent = indent
                in_block = _is_block_line(line)
                continue
            flush()
            group = []
            in_block = False

        if line.startswith("- ") and not _LIST_ITEM_RE.match(line):
            group = [line]
            entry_indent = indent
            in_block = _is_block_line(line)
    flush()
    return entries


def _entry_identity(entry: dict) -> tuple[str, str]:
    """Return (kind, identity) for a composed-tree entry."""
    entry_id = entry.get("id")
    if isinstance(entry_id, str) and entry_id:
        return "entry", entry_id
    name = entry.get("name")
    if isinstance(name, str) and name:
        if name.startswith("file://"):
            return "file", Path(name[len("file://"):]).name
        return "pkg", name
    return "anonymous", "anonymous"


def _entry_source(entry: dict, profile_dir: Optional[Path]) -> str:
    name = entry.get("name")
    if isinstance(name, str) and name.startswith("file://"):
        target = Path(name[len("file://"):])
        if profile_dir is not None:
            try:
                return str(target.relative_to(profile_dir))
            except ValueError:
                return str(target)
        return str(target)
    return name if isinstance(name, str) else ""


def _entry_effects(entry: dict) -> Effects:
    effects = Effects()
    config = entry.get("config")
    if isinstance(config, dict):
        effects.config_sha256 = _sha256_text(_canonical(_stringify(config)))
        context_path = config.get("configPath")
        if isinstance(context_path, str):
            effects.hook_events = list(_HOOK_BRIDGE_EVENTS)
    return effects


#: Events the Claude-format hooks bridge seams onto. Recorded when a lane
#: carries a hooks-bridge loader, because that is what such a lane buys.
_HOOK_BRIDGE_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "Stop", "SubagentStart", "SubagentStop",
)


def _stringify(value):
    if isinstance(value, dict):
        return {str(k): _stringify(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stringify(item) for item in value]
    return value


def parse_diagnostics(stderr_text: str) -> tuple[list[DeadReference], list[str]]:
    """Extract dead patch references and unresolved bundles from dsh stderr."""
    dead: list[DeadReference] = []
    unresolved: list[str] = []
    for line in stderr_text.splitlines():
        match = _DEAD_REF_RE.search(line)
        if match:
            dead.append(DeadReference(patch=match.group("patch"),
                                      entry=match.group("entry")))
        skip = _SKIP_BUNDLE_RE.search(line)
        if skip:
            unresolved.append(skip.group("bundle"))
    return dead, unresolved


# --- discovery ------------------------------------------------------------


def dsh_home(override: Optional[Path] = None) -> Path:
    if override is not None:
        return Path(override)
    env = os.environ.get("DSH_HOME") or os.environ.get("CUPOLA_DSH_HOME")
    if env:
        return Path(env)
    return Path.home() / ".dsh"


def profile_dir(name: str, home: Optional[Path] = None) -> Path:
    return dsh_home(home) / "profiles" / name


def read_profile_package(name: str, home: Optional[Path] = None) -> dict:
    path = profile_dir(name, home) / "package.json"
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise LaneError(f"cannot read profile package.json at {path}: {exc}") from exc
    return loaded if isinstance(loaded, dict) else {}


def run_dump_config(profile: str, home: Optional[Path] = None,
                    patch_paths: Optional[Iterable[Path]] = None,
                    dsh_bin: str = "dsh",
                    timeout: int = DEFAULT_DUMP_TIMEOUT,
                    dump: bool = True) -> tuple[str, str]:
    """Run ``dsh --dump-config`` and return (stdout, stderr).

    ``dump=False`` performs a cheap composability check instead of the full
    dump: the caller supplies the command, so tests can stub it out.
    """
    if not dump:
        return "", ""
    argv = [dsh_bin, "--profile", profile]
    for path in patch_paths or ():
        argv += ["--patch", str(path)]
    argv.append("--dump-config")
    env = dict(os.environ)
    env["DSH_HOME"] = str(dsh_home(home))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except FileNotFoundError as exc:
        raise LaneError(
            f"cannot run '{dsh_bin}': {exc}. Lane discovery needs the dsh CLI "
            "on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise LaneError(
            f"'{dsh_bin} --dump-config' timed out after {timeout}s; the "
            "profile writes into its own directory, so the host must run "
            "unconfined.") from exc
    if "EROFS" in proc.stderr or "read-only file system" in proc.stderr:
        raise LaneError(
            "--dump-config needs write access to the profile directory "
            f"({profile_dir(profile, home)}); it died with EROFS. Run lane "
            "discovery unconfined, or point DSH_HOME at a writable copy.")
    return proc.stdout, proc.stderr


#: File names that carry an always-on ruleset.
RULESET_NAMES = ("AGENTS.md", "CLAUDE.md")


def ruleset_lane(label: str, path: Path, layer: str) -> Lane:
    """A lane for one always-on instruction file."""
    digest = sha256_file(path)
    return Lane(
        lane_id=f"{KIND_RULESET}:{label}",
        kind=KIND_RULESET,
        source=str(path),
        declared=DECLARED_ACTIVE if path.is_file() else DECLARED_MISSING,
        source_hash=None if digest.startswith("unreadable") else digest,
        layer=layer,
        detail=f"{path.stat().st_size} bytes" if path.is_file() else "absent",
    )


def _skill_name(path: Path) -> str:
    """Skill name from YAML frontmatter, falling back to the directory name."""
    if path.name == "SKILL.md":
        try:
            head = path.read_text(errors="replace")[:2000]
        except OSError:
            return path.parent.name
        match = re.search(r"^name:\s*(.+)$", head, re.MULTILINE)
        if match:
            return match.group(1).strip().strip("\"'")
        return path.parent.name
    return path.stem


def skill_lanes(skill_dirs: Iterable[Path], layer: str) -> list[Lane]:
    """One lane per skill file, so a skill change is visible on its own."""
    lanes: list[Lane] = []
    for root in skill_dirs:
        root = Path(root)
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in (".md", ".yaml", ".yml"):
                continue
            if path.name not in ("SKILL.md",) and path.parent != root:
                continue
            name = _skill_name(path)
            lanes.append(Lane(
                lane_id=f"{KIND_SKILL}:{name}",
                kind=KIND_SKILL,
                source=str(path),
                source_hash=sha256_file(path),
                layer=layer,
                detail=f"{path.stat().st_size} bytes",
                effects=Effects(skills=[name]),
            ))
    return lanes


def mcp_lanes(config_path: Path, layer: str = LAYER_INVENTORY) -> list[Lane]:
    """One lane per configured MCP server."""
    if not config_path.is_file():
        return []
    try:
        loaded = json.loads(config_path.read_text())
    except (OSError, ValueError):
        return []
    servers = loaded.get("mcpServers") if isinstance(loaded, dict) else None
    if not isinstance(servers, dict):
        return []
    lanes: list[Lane] = []
    for name, spec in sorted(servers.items()):
        command = ""
        if isinstance(spec, dict):
            command = str(spec.get("command", ""))
        lanes.append(Lane(
            lane_id=f"{KIND_MCP}:{name}",
            kind=KIND_MCP,
            source=command or str(config_path),
            source_hash=_sha256_text(_canonical(spec)),
            layer=layer,
            detail=f"from {config_path.name}",
            effects=Effects(mcp_servers=[name]),
        ))
    return lanes


def collect(
    profile: str,
    home: Optional[Path] = None,
    workspace: Optional[Path] = None,
    patch_paths: Optional[Iterable[Path]] = None,
    dsh_bin: str = "dsh",
    timeout: int = DEFAULT_DUMP_TIMEOUT,
    dump: bool = True,
    extra_skill_dirs: Iterable[Path] = (),
) -> LaneSet:
    """Discover every installed lane for one dsh profile.

    Never raises for a missing optional source — the failures land in
    ``errors`` or ``warnings()`` so a partial inventory is still usable and
    visibly partial.
    """
    home = dsh_home(home)
    workspace = Path(workspace) if workspace else Path.cwd()
    lane_set = LaneSet(dsh_home=str(home), profile=profile)

    package = read_profile_package(profile, home)
    profile_block = (package.get("dsh") or {}).get("profile") or {}
    bundles = profile_block.get("bundles") or []
    dependencies = package.get("dependencies") or {}

    for name in bundles:
        lane_set.lanes.append(Lane(
            lane_id=f"{KIND_BUNDLE}:{name}",
            kind=KIND_BUNDLE,
            source=name,
            source_hash=sha256_file(profile_dir(profile, home) / "package.json"),
            layer=LAYER_BUNDLE,
            detail="declared in dsh.profile.bundles",
        ))

    for name, spec in sorted(dependencies.items()):
        version = str(spec)
        digest = None
        if version.startswith(("link:", "file:")):
            digest = sha256_tree(Path(version.split(":", 1)[1]))
        lane_set.lanes.append(Lane(
            lane_id=f"{KIND_DEPENDENCY}:{name}",
            kind=KIND_DEPENDENCY,
            source=f"{name}@{version}",
            source_hash=digest,
            layer=LAYER_BUNDLE,
            detail="linked source tree" if version.startswith(("link:", "file:"))
                   else version,
        ))

    try:
        stdout, stderr = run_dump_config(profile, home, patch_paths, dsh_bin,
                                         timeout, dump)
    except LaneError as exc:
        lane_set.errors.append(str(exc))
        stdout, stderr = "", ""

    if stdout:
        lane_set.lanes.extend(_lanes_from_dump(stdout, profile, home))

    if stderr:
        dead, unresolved = parse_diagnostics(stderr)
        lane_set.dead_references = dead
        lane_set.unresolved_bundles = unresolved

    skill_dirs = [home / "skills", workspace / ".dsh" / "skills"]
    skill_dirs += [Path(p) for p in extra_skill_dirs]
    lane_set.lanes.extend(skill_lanes(skill_dirs, LAYER_INVENTORY))

    lane_set.lanes.append(ruleset_lane("home", home / "AGENTS.md", LAYER_INVENTORY))
    lane_set.lanes.append(ruleset_lane("workspace",
                                       workspace / "AGENTS.md", LAYER_INVENTORY))

    lane_set.lanes.extend(mcp_lanes(workspace / "mcp.json"))

    _dedupe(lane_set)
    return lane_set


def _patch_inserted(entry: dict) -> bool:
    """True when the patch added this entry rather than reconfiguring one."""
    return entry.get("id") is None


def _third_party_layer(entry: dict) -> bool:
    """True when the entry comes from a non-first-party bundle.

    ``_layer`` is the bundle that introduced the entry. Shipped bundles are
    package names (``@deepseek-ai/dsh-base``); a third-party plugin declares
    itself as a bare name (``dsh-doctor``). Only third-party bundles are worth
    a lane of their own — a config override of a shipped entry is not a knob
    anyone toggles.
    """
    layer = entry.get("_layer")
    if not isinstance(layer, str) or not layer:
        return False
    return "@" not in layer and "/" not in layer


def _is_lane_entry(entry: dict) -> bool:
    if entry.get("_patch") is None:
        return False
    return _patch_inserted(entry) or _third_party_layer(entry)


def _lanes_from_dump(text: str, profile: str, home: Path) -> list[Lane]:
    """Turn a composed tree into one lane per independently toggleable entry.

    A bundle lane already covers everything a bundle contributes, so only an
    inserted row or a patched third-party bundle becomes a lane of its own.
    A bundle and a patch entry are genuinely different knobs: the bundle is a
    package line, the entry is a loader row an overlay can disable.
    """
    entries = parse_dump(text)
    lanes: list[Lane] = []
    profile_root = profile_dir(profile, home)

    for entry in entries:
        if not _is_lane_entry(entry):
            continue

        _kind, identity = _entry_identity(entry)
        patch_ref = entry["_patch"]
        layer = LAYER_PROFILE_PATCH if "profiles/" in patch_ref \
            else LAYER_HOME_PATCH
        source = _entry_source(entry, profile_root)
        lanes.append(Lane(
            lane_id=f"{KIND_PATCH_ENTRY}:{identity}",
            kind=KIND_PATCH_ENTRY,
            source=source or identity,
            declared=DECLARED_DISABLED if entry.get("disabled") is True
            else DECLARED_ACTIVE,
            source_hash=_sha256_text(
                _canonical(_stringify(entry.get("config") or {}))),
            layer=layer,
            enabled_by=patch_ref,
            detail=f"{entry.get('_layer')} via {Path(patch_ref).name}; "
                   f"{'inserted' if _patch_inserted(entry) else 'patched'}",
            effects=_entry_effects(entry),
        ))
    return lanes


def _dedupe(lane_set: LaneSet) -> None:
    """Keep one record per lane id: the composed tree beats the declaration."""
    seen: dict[str, Lane] = {}
    for lane in lane_set.lanes:
        existing = seen.get(lane.lane_id)
        if existing is None or lane.kind == KIND_PATCH_ENTRY:
            seen[lane.lane_id] = lane
    lane_set.lanes = sorted(seen.values(), key=lambda item: item.lane_id)


# --- snapshot, diff, ledger ----------------------------------------------


def snapshot_path(state_dir: Optional[Path] = None) -> Path:
    base = state_dir or _default_state_dir()
    return Path(base) / "lanes"


def _default_state_dir() -> Path:
    env = os.environ.get("CUPOLA_BENCH_STATE")
    if env:
        return Path(env)
    from cupola.config import DEFAULT_STATE_DIR
    return DEFAULT_STATE_DIR / "bench"


def write_snapshot(lane_set: LaneSet, out_dir: Optional[Path] = None) -> Path:
    """Write a lane snapshot. Names carry sub-second precision so two
    snapshots taken in the same second never overwrite each other."""
    directory = Path(out_dir) if out_dir else snapshot_path()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(lane_set.generated_at))
    micros = int(lane_set.generated_at % 1 * 1_000_000)
    path = directory / f"{lane_set.profile}-{stamp}-{micros:06d}.json"
    if path.exists():
        path = directory / f"{lane_set.profile}-{stamp}-{micros:06d}-" \
                          f"{lane_set.lane_set_hash()[:8]}.json"
    path.write_text(json.dumps(lane_set.to_dict(), indent=2) + "\n")
    return path


def read_snapshot(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise LaneError(f"cannot read lane snapshot {path}: {exc}") from exc


@dataclass
class LaneChange:
    """One difference between two inventories."""

    lane_id: str
    change: str  # added | removed | enabled | disabled | changed
    fields: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"lane_id": self.lane_id, "change": self.change,
                "fields": self.fields}


def diff_snapshots(before: dict, after: dict) -> list[LaneChange]:
    """Per-lane differences, including what changed inside a lane."""
    old = {lane["lane_id"]: lane for lane in before.get("lanes") or []}
    new = {lane["lane_id"]: lane for lane in after.get("lanes") or []}
    changes: list[LaneChange] = []

    for lane_id in sorted(set(old) | set(new)):
        if lane_id not in old:
            changes.append(LaneChange(lane_id, "added"))
            continue
        if lane_id not in new:
            changes.append(LaneChange(lane_id, "removed"))
            continue
        left, right = old[lane_id], new[lane_id]
        if left.get("declared") != right.get("declared"):
            change = "enabled" if right.get("declared") == DECLARED_ACTIVE \
                else "disabled"
            changes.append(LaneChange(lane_id, change, {
                "declared": [left.get("declared"), right.get("declared")]}))
            continue
        fields = {}
        for key in ("source", "source_hash"):
            if left.get(key) != right.get(key):
                fields[key] = [left.get(key), right.get(key)]
        for key, value in (left.get("effects") or {}).items():
            if value != (right.get("effects") or {}).get(key):
                fields[f"effects.{key}"] = [value,
                                            (right.get("effects") or {}).get(key)]
        if fields:
            changes.append(LaneChange(lane_id, "changed", fields))

    return changes


def ledger_path(state_dir: Optional[Path] = None) -> Path:
    base = state_dir or _default_state_dir()
    return Path(base) / "ledger.jsonl"


def append_ledger(entry: dict, state_dir: Optional[Path] = None) -> Path:
    """Append one run record. History is what turns drift into a named cause."""
    path = ledger_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(_canonical(entry) + "\n")
    return path


def read_ledger(state_dir: Optional[Path] = None) -> list[dict]:
    path = ledger_path(state_dir)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


# --- activation gates -----------------------------------------------------


@dataclass
class Gate:
    """A declared, checkable effect that proves a lane actually activated."""

    lane_id: str
    kind: str  # file_exists | file_contains | context_hash | always
    path: Optional[str] = None
    contains: Optional[str] = None
    sha256: Optional[str] = None

    def to_dict(self) -> dict:
        return {"lane_id": self.lane_id, "kind": self.kind, "path": self.path,
                "contains": self.contains, "sha256": self.sha256}


def evaluate_gate(gate: Gate, workspace: Path) -> Observation:
    """Check one gate against a finished run's workspace."""
    workspace = Path(workspace)
    if gate.kind == "always":
        return Observation(status=OBSERVED_OK, detail="unconditional",
                           at=time.time())
    if gate.kind == "file_exists":
        target = _gate_target(gate, workspace)
        if target is None:
            return Observation(status=OBSERVED_FAILED,
                               detail="gate has no path", at=time.time())
        if target.is_file():
            digest = sha256_file(target)
            if gate.sha256 and digest != gate.sha256:
                return Observation(
                    status=OBSERVED_NO_OP, at=time.time(),
                    detail=f"{target} present but hash {digest[:12]} != "
                           f"{gate.sha256[:12]}")
            return Observation(status=OBSERVED_OK, at=time.time(),
                               observed={"path": str(target), "sha256": digest},
                               detail=str(target))
        return Observation(status=OBSERVED_NO_OP, at=time.time(),
                           detail=f"{target} was not written")
    if gate.kind == "file_contains":
        target = _gate_target(gate, workspace)
        if target is None or not target.is_file():
            return Observation(status=OBSERVED_NO_OP, at=time.time(),
                               detail=f"{target} missing")
        try:
            text = target.read_text(errors="replace")
        except OSError as exc:
            return Observation(status=OBSERVED_FAILED, at=time.time(),
                               detail=str(exc))
        if gate.contains and gate.contains in text:
            return Observation(status=OBSERVED_OK, at=time.time(),
                               detail=f"matched {gate.contains!r}")
        return Observation(status=OBSERVED_NO_OP, at=time.time(),
                           detail=f"{target} lacks {gate.contains!r}")
    return Observation(status=OBSERVED_FAILED, at=time.time(),
                       detail=f"unknown gate kind {gate.kind!r}")


def _gate_target(gate: Gate, workspace: Path) -> Optional[Path]:
    if not gate.path:
        return None
    target = Path(gate.path)
    return target if target.is_absolute() else workspace / target


def apply_observations(lane_set: LaneSet, gates: Iterable[Gate],
                       workspace: Path) -> LaneSet:
    """Attach run-time evidence to a lane set, marking unproven lanes no_op."""
    gated = {gate.lane_id for gate in gates}
    for gate in gates:
        lane = lane_set.by_id().get(gate.lane_id)
        if lane is None:
            lane_set.errors.append(
                f"gate names unknown lane {gate.lane_id!r}")
            continue
        lane.observation = evaluate_gate(gate, workspace)
    for lane in lane_set.lanes:
        if lane.lane_id in gated:
            continue
        if lane.declared == DECLARED_ACTIVE and lane.kind in (
                KIND_PATCH_ENTRY, KIND_SKILL, KIND_RULESET):
            lane.observation = Observation(
                status=OBSERVED_NO_OP, at=time.time(),
                detail="no activation gate declared")
    return lane_set


def creditworthy(lane_set: LaneSet) -> list[Lane]:
    """Lanes whose effect may be credited: active and proven, or ungated types.

    A patch-entry, skill or ruleset lane that was never proven active is
    excluded, because a silent hook failure would otherwise be reported as
    "this plugin changes nothing" — a false null result.
    """
    credited: list[Lane] = []
    for lane in lane_set.lanes:
        if lane.observation.status == OBSERVED_NO_OP:
            continue
        if lane.observation.status == OBSERVED_FAILED:
            continue
        credited.append(lane)
    return credited
