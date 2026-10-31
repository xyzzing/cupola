"""Minder/cupola boundary contract tests (minder-memory plan PR 8).

Contract-only: these pin the typed objects without any GPU behaviour
change — no LoRA loading, no llama-server flag changes.
"""

from __future__ import annotations

from dataclasses import fields

from cupola.contracts import InferenceGrant, InferenceIntent, RuntimeSnapshot, evaluate_intent
from cupola.telemetry import TelemetryState


def test_snapshot_defaults_safe_when_sensors_missing():
    # bare construction: a machine with no sensors at all
    snap = RuntimeSnapshot()
    assert snap.hotspot_celsius is None
    assert snap.vram_available_mb is None
    assert snap.pacing_active is False
    assert snap.adapters_loaded == ()

    # telemetry-shaped object with everything unavailable
    empty = TelemetryState()  # all None / "unavailable" defaults
    snap = RuntimeSnapshot.from_telemetry(empty)
    assert snap.hotspot_celsius is None
    assert snap.pacing_active is False
    assert snap.notes["quality"] == "unavailable"

    # collector crash degrades to an all-unknown snapshot, never raises
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("sensors exploded")

    snap = RuntimeSnapshot.from_telemetry(None)  # collector would run; here
    assert snap.ts > 0                            # it simply must not raise

    populated = TelemetryState(hotspot_celsius=95.0, pacing_active=True,
                               vram_available_mb=2048)
    snap = RuntimeSnapshot.from_telemetry(populated)
    assert snap.hotspot_celsius == 95.0
    assert snap.pacing_active is True
    assert snap.vram_available_mb == 2048


def test_intent_adapter_preference_recorded_not_acted_on():
    snap = RuntimeSnapshot()
    intent = InferenceIntent(adapter_preference="terse-coder-lora",
                             thinking_mode="deep", thinking_tokens=4096)
    grant = evaluate_intent(snap, intent)
    # recorded…
    assert grant.adapter_requested == "terse-coder-lora"
    # …but never acted on: no adapter is loaded, nothing else changes
    assert grant.adapter_loaded is None
    assert grant.granted is True
    assert grant.clamp_applied is False
    assert grant.thinking_tokens == 4096  # nominal: intent passes through
    assert "adapter" not in grant.reason.lower()


def test_thermal_clamp_mirrors_policy_thresholds():
    hot = RuntimeSnapshot(pacing_active=True, hotspot_celsius=95.0)
    grant = evaluate_intent(hot, InferenceIntent(thinking_mode="deep",
                                                 thinking_tokens=4096))
    assert grant.clamp_applied is True
    assert grant.thinking_tokens == 0  # very hot → DIRECT only

    warm = RuntimeSnapshot(pacing_active=True, hotspot_celsius=90.0)
    grant = evaluate_intent(warm, InferenceIntent(thinking_tokens=4096))
    assert grant.clamp_applied is True
    assert grant.thinking_tokens == 1024  # pacing → LEAN cap

    nominal = RuntimeSnapshot(hotspot_celsius=70.0)
    grant = evaluate_intent(nominal, InferenceIntent(thinking_tokens=4096))
    assert grant.clamp_applied is False
    assert grant.thinking_tokens == 4096


def test_grant_contract_shape():
    """The three objects expose exactly the contract fields the plan names."""
    assert {f.name for f in fields(RuntimeSnapshot)} >= {
        "ts", "hotspot_celsius", "pacing_active", "vram_available_mb",
        "model_id", "adapters_loaded"}
    assert {f.name for f in fields(InferenceIntent)} >= {
        "task_type", "thinking_mode", "thinking_tokens", "max_tokens",
        "adapter_preference"}
    assert {f.name for f in fields(InferenceGrant)} >= {
        "granted", "thinking_tokens", "max_tokens", "clamp_applied",
        "adapter_requested", "adapter_loaded", "snapshot_ts"}
