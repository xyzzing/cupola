"""Tests for Minder cognitive policy engine."""


import pytest

from cupola.minder import (
    TaskType,
    ThinkingMode,
    evaluate_policy,
)
from cupola.telemetry import TelemetryState


def test_policy_planning_deep_mode():
    """Planning tasks get DEEP mode (4096 thinking tokens)."""
    telemetry = TelemetryState(
        vram_available_mb=4096,
        vram_headroom_mb=1536,
        hotspot_celsius=70.0,
        pacing_active=False,
        gtt_spill_detected=False,
        timestamp=0.0,
    )

    decision = evaluate_policy(telemetry, TaskType.PLANNING)
    assert decision.mode == ThinkingMode.DEEP
    assert decision.thinking_tokens == 4096


def test_policy_ingestion_lean_mode():
    """Ingestion tasks get LEAN mode (512 thinking tokens)."""
    telemetry = TelemetryState(
        vram_available_mb=4096,
        vram_headroom_mb=1536,
        hotspot_celsius=70.0,
        pacing_active=False,
        gtt_spill_detected=False,
        timestamp=0.0,
    )

    decision = evaluate_policy(telemetry, TaskType.INGESTION)
    assert decision.mode == ThinkingMode.LEAN
    assert decision.thinking_tokens == 512


def test_policy_execution_lean_mode():
    """Execution tasks get LEAN mode (1024 thinking tokens)."""
    telemetry = TelemetryState(
        vram_available_mb=4096,
        vram_headroom_mb=1536,
        hotspot_celsius=70.0,
        pacing_active=False,
        gtt_spill_detected=False,
        timestamp=0.0,
    )

    decision = evaluate_policy(telemetry, TaskType.EXECUTION)
    assert decision.mode == ThinkingMode.LEAN
    assert decision.thinking_tokens == 1024


def test_policy_verification_direct_mode():
    """Verification tasks get DIRECT mode (0 thinking tokens)."""
    telemetry = TelemetryState(
        vram_available_mb=4096,
        vram_headroom_mb=1536,
        hotspot_celsius=70.0,
        pacing_active=False,
        gtt_spill_detected=False,
        timestamp=0.0,
    )

    decision = evaluate_policy(telemetry, TaskType.VERIFICATION)
    assert decision.mode == ThinkingMode.DIRECT
    assert decision.thinking_tokens == 0


def test_policy_thermal_pacing_lean():
    """Thermal pacing clamps to LEAN mode."""
    telemetry = TelemetryState(
        vram_available_mb=4096,
        vram_headroom_mb=1536,
        hotspot_celsius=90.0,  # > 88°C
        pacing_active=True,
        gtt_spill_detected=False,
        timestamp=0.0,
    )

    decision = evaluate_policy(telemetry, TaskType.PLANNING)
    # Should be clamped to LEAN (not DEEP)
    assert decision.mode == ThinkingMode.LEAN
    assert decision.pre_dispatch_delay > 0


def test_policy_thermal_pacing_direct():
    """Very high thermal pacing clamps to DIRECT mode."""
    telemetry = TelemetryState(
        vram_available_mb=4096,
        vram_headroom_mb=1536,
        hotspot_celsius=95.0,  # > 92°C
        pacing_active=True,
        gtt_spill_detected=False,
        timestamp=0.0,
    )

    decision = evaluate_policy(telemetry, TaskType.PLANNING)
    # Should be clamped to DIRECT
    assert decision.mode == ThinkingMode.DIRECT
    assert decision.pre_dispatch_delay > 0


def test_policy_vram_pacing():
    """Low VRAM triggers pacing."""
    telemetry = TelemetryState(
        vram_available_mb=1000,  # < 1.5GB
        vram_headroom_mb=-1560,
        hotspot_celsius=70.0,
        pacing_active=True,
        gtt_spill_detected=False,
        timestamp=0.0,
    )

    decision = evaluate_policy(telemetry, TaskType.PLANNING)
    assert decision.mode == ThinkingMode.LEAN


def test_policy_sampling_params():
    """Policy includes sampling parameters."""
    telemetry = TelemetryState(
        vram_available_mb=4096,
        vram_headroom_mb=1536,
        hotspot_celsius=70.0,
        pacing_active=False,
        gtt_spill_detected=False,
        timestamp=0.0,
    )

    decision = evaluate_policy(telemetry, TaskType.PLANNING)
    assert 0.0 <= decision.temperature <= 1.0
    assert 0.0 <= decision.top_p <= 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
