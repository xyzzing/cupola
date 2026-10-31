"""Tests for the toolchain lane inventory.

Lane discovery must be generated, never a hand-maintained registry, and it
must distinguish "declared active" from "provably did something". These tests
use a synthetic DSH_HOME built in a temp directory plus checked-in composed
tree fixtures, so they never touch the live profiles and never need dsh.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cupola.lanes import (
    DECLARED_ACTIVE,
    DECLARED_DISABLED,
    KIND_BUNDLE,
    KIND_DEPENDENCY,
    KIND_MCP,
    KIND_PATCH_ENTRY,
    KIND_RULESET,
    KIND_SKILL,
    OBSERVED_NO_OP,
    OBSERVED_OK,
    Gate,
    Lane,
    LaneError,
    LaneSet,
    append_ledger,
    apply_observations,
    collect,
    creditworthy,
    diff_snapshots,
    evaluate_gate,
    parse_diagnostics,
    parse_dump,
    read_ledger,
    sha256_tree,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lanes"
WEB_TREE = FIXTURES / "composed-web.yml"

PROFILE = "cupolatest"


# --- helpers --------------------------------------------------------------


def _make_home(root: Path, *, skill_body: str = "# Skill\n") -> Path:
    """A synthetic DSH_HOME with one profile and supporting lanes."""
    home = root / "dsh"
    profile = home / "profiles" / PROFILE
    (profile / "node_modules").mkdir(parents=True)
    (profile / "package.json").write_text(json.dumps({
        "name": f"dsh-profile-{PROFILE}",
        "private": True,
        "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "acme-widget"],
                            "patchReload": "startup"}},
        "dependencies": {
            "@deepseek-ai/dsh-hooks-claude-code": "0.1.5-rc.2",
            "acme-linked": "link:./vendor/acme-linked",
        },
    }))
    (profile / "cordis.patch.yml").write_text(
        "- insert:\n    - name: ./bridge-loader.mjs\n"
        "      config:\n        configPath: /tmp/hooks.json\n")
    (home / "skills" / "sample").mkdir(parents=True)
    (home / "skills" / "sample" / "SKILL.md").write_text(skill_body)
    (home / "AGENTS.md").write_text("# Rules\n")
    vendor = profile / "vendor" / "acme-linked"
    vendor.mkdir(parents=True)
    (vendor / "index.js").write_text("export const x = 1;\n")
    return home


# --- composed-tree parsing ------------------------------------------------


def test_parse_dump_counts_and_marks_layers():
    entries = parse_dump(WEB_TREE.read_text())
    assert len(entries) == 80
    patched = [e for e in entries if e.get("_patch")]
    assert patched, "fixture must contain patch-decided entries"
    for entry in patched:
        assert isinstance(entry["_patch"], str)
        assert not entry["_patch"].startswith("/home/"), \
            "fixture must not leak host paths"


def test_parse_dump_reads_folded_scalar_names():
    """"name: >-" carries a file:// URI on the following line."""
    entries = parse_dump(WEB_TREE.read_text())
    names = [e.get("name") for e in entries]
    assert any(str(n).startswith("file://") for n in names), names[:5]
    bridge = next(e for e in entries
                  if str(e.get("name", "")).endswith("ponytail-bridge-loader.mjs"))
    assert bridge["config"]["defaultTimeoutMs"] == 5000
    assert bridge["config"]["pluginRoot"].endswith("share/ponytail")


def test_parse_dump_keeps_nested_collections_verbatim():
    """Nested sequences are not interpreted, but must stay hashable."""
    entries = parse_dump(WEB_TREE.read_text())
    mcp = next(e for e in entries if e.get("id") == "mcp-citra")
    items = mcp["config"]["args"]["_items"]
    assert any("sylphx/citra" in item for item in items)


def test_parse_dump_is_deterministic_and_serialisable():
    first = parse_dump(WEB_TREE.read_text())
    second = parse_dump(WEB_TREE.read_text())
    assert first == second
    json.dumps(first)  # must not raise


def test_parse_diagnostics_finds_dead_references():
    dead, unresolved = parse_diagnostics(
        (FIXTURES / "diagnostics-web.stderr").read_text())
    assert {ref.entry for ref in dead} == {"web-ui-better-sidebar",
                                           "captain-guard"}
    assert unresolved == []


def test_parse_diagnostics_finds_unresolved_bundles():
    dead, unresolved = parse_diagnostics(
        (FIXTURES / "diagnostics-headless.stderr").read_text())
    assert dead == []
    assert unresolved == ["dsh-context-guard", "dsh-task-budget-gate"]


# --- lane extraction from a composed tree ---------------------------------


def test_lanes_from_dump_selects_only_real_knobs():
    text = WEB_TREE.read_text()
    lane_set = LaneSet(dsh_home="/TMP-DSH", profile="web",
                       lanes=_dump_lanes(text))
    ids = {lane.lane_id.split(":", 1)[1] for lane in lane_set.lanes}
    # Inserted loader rows are lanes.
    assert "ponytail-bridge-loader.mjs" in ids
    assert "minder-bridge-loader.mjs" in ids
    # A patched third-party bundle is a lane.
    assert "dsh-doctor" in ids
    # Reconfiguring a shipped entry is not: it is not a toggleable knob.
    assert "permission" not in ids
    assert "tool-bash" not in ids
    assert "mcp-citra" not in ids


def _dump_lanes(text: str, profile_dir: Path = Path("/TMP-DSH/profiles/web")):
    from cupola.lanes import _lanes_from_dump
    return _lanes_from_dump(text, "web", profile_dir.parent.parent)


def test_patch_entry_records_its_deciding_layer():
    lanes = {lane.lane_id.split(":", 1)[1]: lane
             for lane in _dump_lanes(WEB_TREE.read_text())}
    pony = lanes["ponytail-bridge-loader.mjs"]
    assert pony.declared == DECLARED_ACTIVE
    assert pony.enabled_by.endswith("cordis.patch.yml")
    assert pony.effects.hook_events, "a hooks bridge must declare its events"
    assert pony.effects.config_sha256
    doctor = lanes["dsh-doctor"]
    assert doctor.declared == DECLARED_DISABLED


# --- full collection over a synthetic home --------------------------------


def test_collect_discovers_every_lane_kind(tmp_path):
    home = _make_home(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "mcp.json").write_text(json.dumps(
        {"mcpServers": {"citra": {"command": "npx", "args": ["-y", "x"]}}}))
    lane_set = collect(PROFILE, home=home, workspace=workspace, dump=False)
    kinds = {lane.kind for lane in lane_set.lanes}
    assert kinds == {KIND_BUNDLE, KIND_DEPENDENCY, KIND_SKILL, KIND_RULESET,
                     KIND_MCP}
    assert lane_set.errors == []


def test_collect_is_deterministic(tmp_path):
    home = _make_home(tmp_path)
    workspace = tmp_path
    first = collect(PROFILE, home=home, workspace=workspace, dump=False)
    second = collect(PROFILE, home=home, workspace=workspace, dump=False)
    assert first.lane_set_hash() == second.lane_set_hash()
    assert first.to_dict()["lanes"] == second.to_dict()["lanes"]


def test_lane_ids_are_stable_across_rescans(tmp_path):
    home = _make_home(tmp_path)
    before = {lane.lane_id for lane in
              collect(PROFILE, home=home, workspace=tmp_path, dump=False).lanes}
    after = {lane.lane_id for lane in
             collect(PROFILE, home=home, workspace=tmp_path, dump=False).lanes}
    assert before == after


def test_hash_changes_when_a_skill_changes(tmp_path):
    home = _make_home(tmp_path, skill_body="# Skill\noriginal\n")
    workspace = tmp_path
    before = collect(PROFILE, home=home, workspace=workspace, dump=False)
    (home / "skills" / "sample" / "SKILL.md").write_text("# Skill\nedited\n")
    after = collect(PROFILE, home=home, workspace=workspace, dump=False)
    assert before.lane_set_hash() != after.lane_set_hash()
    changes = diff_snapshots(before.to_dict(), after.to_dict())
    assert [c.lane_id for c in changes] == ["skill:sample"]
    assert changes[0].change == "changed"
    assert "source_hash" in changes[0].fields


def test_hash_changes_when_a_lane_is_disabled(tmp_path):
    home = _make_home(tmp_path)
    workspace = tmp_path
    before = collect(PROFILE, home=home, workspace=workspace, dump=False)
    after = LaneSet(**{**before.__dict__})
    after.lanes = [Lane(**{**lane.__dict__,
                           "declared": DECLARED_DISABLED})
                   if lane.kind == KIND_MCP else lane
                   for lane in before.lanes]
    # No MCP lane here: assert on a real one instead.
    after = LaneSet(**{**before.__dict__})
    after.lanes = [
        Lane(**{**lane.__dict__, "declared": DECLARED_DISABLED})
        if lane.kind == KIND_BUNDLE else lane
        for lane in before.lanes
    ]
    assert before.lane_set_hash() != after.lane_set_hash()
    changes = diff_snapshots(before.to_dict(), after.to_dict())
    assert all(change.change == "disabled" for change in changes)
    assert changes


def test_sha256_tree_ignores_dependency_caches(tmp_path):
    tree = tmp_path / "plugin"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "index.js").write_text("a\n")
    (tree / "node_modules" / "junk").mkdir(parents=True)
    (tree / "node_modules" / "junk" / "big.js").write_text("b" * 100)
    baseline = sha256_tree(tree)
    (tree / "node_modules" / "junk" / "big.js").write_text("c" * 100)
    assert sha256_tree(tree) == baseline
    (tree / "src" / "index.js").write_text("a changed\n")
    assert sha256_tree(tree) != baseline


def test_sha256_tree_reports_missing_path(tmp_path):
    assert sha256_tree(tmp_path / "nope") == "missing"


# --- diffing and the ledger ----------------------------------------------


def test_diff_reports_added_removed_and_changed():
    before = LaneSet(lanes=[Lane(lane_id="skill:a", kind=KIND_SKILL,
                                 source_hash="1"),
                            Lane(lane_id="skill:b", kind=KIND_SKILL,
                                 source_hash="1")]).to_dict()
    after = LaneSet(lanes=[Lane(lane_id="skill:a", kind=KIND_SKILL,
                                source_hash="2"),
                           Lane(lane_id="skill:c", kind=KIND_SKILL,
                                source_hash="1")]).to_dict()
    changes = {c.lane_id: c for c in diff_snapshots(before, after)}
    assert changes["skill:a"].change == "changed"
    assert changes["skill:b"].change == "removed"
    assert changes["skill:c"].change == "added"


def test_snapshot_roundtrip_and_ledger(tmp_path):
    lane_set = LaneSet(profile="web", lanes=[
        Lane(lane_id="install:ponytail", kind=KIND_PATCH_ENTRY, source="x")])
    state = tmp_path / "state"
    snapshot = tmp_path / "snap.json"
    snapshot.write_text(json.dumps(lane_set.to_dict()))
    from cupola.lanes import read_snapshot
    assert read_snapshot(snapshot)["lane_set_hash"] == lane_set.lane_set_hash()

    append_ledger({"run_id": "r1", "lane_set_hash": lane_set.lane_set_hash()},
                  state_dir=state)
    append_ledger({"run_id": "r2", "lane_set_hash": "other"}, state_dir=state)
    rows = read_ledger(state_dir=state)
    assert [row["run_id"] for row in rows] == ["r1", "r2"]


def test_read_snapshot_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    from cupola.lanes import read_snapshot
    with pytest.raises(LaneError):
        read_snapshot(bad)


# --- activation gates: declared is not observed ---------------------------


def test_gate_ok_when_marker_file_present(tmp_path):
    marker = tmp_path / ".ponytail" / ".ponytail-active"
    marker.parent.mkdir(parents=True)
    marker.write_text("full")
    observation = evaluate_gate(
        Gate(lane_id="install:ponytail", kind="file_exists",
             path=".ponytail/.ponytail-active"), tmp_path)
    assert observation.status == OBSERVED_OK
    assert observation.observed["path"].endswith(".ponytail-active")


def test_gate_reports_no_op_when_lane_did_nothing(tmp_path):
    """A silently inert hook must not look like a successful run."""
    observation = evaluate_gate(
        Gate(lane_id="install:ponytail", kind="file_exists",
             path=".ponytail/.ponytail-active"), tmp_path)
    assert observation.status == OBSERVED_NO_OP
    assert "was not written" in observation.detail


def test_gate_hash_mismatch_is_a_no_op(tmp_path):
    (tmp_path / "ctx.txt").write_text("actual")
    observation = evaluate_gate(
        Gate(lane_id="ruleset:home", kind="file_exists", path="ctx.txt",
             sha256="0" * 64), tmp_path)
    assert observation.status == OBSERVED_NO_OP
    assert "!=" in observation.detail


def test_gate_file_contains(tmp_path):
    (tmp_path / "AGENTS.md").write_text("PONYTAIL:OFF\n")
    ok = evaluate_gate(
        Gate(lane_id="ruleset:home", kind="file_contains", path="AGENTS.md",
             contains="PONYTAIL:OFF"), tmp_path)
    miss = evaluate_gate(
        Gate(lane_id="ruleset:home", kind="file_contains", path="AGENTS.md",
             contains="lazy senior developer"), tmp_path)
    assert ok.status == OBSERVED_OK
    assert miss.status == OBSERVED_NO_OP


def test_ungated_active_lane_is_not_creditworthy(tmp_path):
    """The whole point: an unproven lane cannot be credited with an effect."""
    lane_set = LaneSet(lanes=[
        Lane(lane_id="install:ponytail", kind=KIND_PATCH_ENTRY, source="x"),
        Lane(lane_id="bundle:dshmarket", kind=KIND_BUNDLE, source="y"),
    ])
    apply_observations(lane_set, [], tmp_path)
    observed = lane_set.by_id()
    assert observed["install:ponytail"].observation.status == OBSERVED_NO_OP
    assert observed["bundle:dshmarket"].observation.status != OBSERVED_NO_OP
    credited = {lane.lane_id for lane in creditworthy(lane_set)}
    assert "install:ponytail" not in credited
    assert "bundle:dshmarket" in credited


def test_gate_names_unknown_lane_is_an_error(tmp_path):
    lane_set = LaneSet(lanes=[Lane(lane_id="install:a", kind=KIND_PATCH_ENTRY)])
    apply_observations(lane_set, [Gate(lane_id="install:ghost",
                                       kind="always")], tmp_path)
    assert any("unknown lane" in err for err in lane_set.errors)


def test_warnings_include_no_op_lanes(tmp_path):
    lane_set = LaneSet(lanes=[
        Lane(lane_id="install:ponytail", kind=KIND_PATCH_ENTRY, source="x")])
    apply_observations(lane_set, [], tmp_path)
    assert any("did not activate" in warning
               for warning in lane_set.warnings())
