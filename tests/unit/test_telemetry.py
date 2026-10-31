"""Tests for Sentinel telemetry."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from cupola.telemetry import (
    SentinelSampler,
    TelemetryState,
    collect_telemetry,
    compute_energy_tier3,
    update_state_atomic,
)


def test_update_state_atomic():
    """Atomic state update via temp file + rename."""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "state.json"
        data = {"test": "value", "number": 42}

        update_state_atomic(data, target)

        # Verify file was written
        assert target.exists()
        with open(target, "r") as f:
            loaded = json.load(f)
        assert loaded == data


def test_update_state_atomic_no_partial_reads():
    """No partial reads during atomic update."""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "state.json"
        # Write initial state
        update_state_atomic({"initial": True}, target)

        # Update atomically
        update_state_atomic({"updated": True, "data": "new"}, target)

        # Should read complete updated state
        with open(target, "r") as f:
            loaded = json.load(f)
        assert loaded["updated"] is True
        assert "initial" not in loaded


def test_telemetry_state_dataclass():
    """TelemetryState dataclass fields."""
    state = TelemetryState(
        vram_available_mb=4096,
        vram_headroom_mb=1536,
        hotspot_celsius=75.0,
        pacing_active=False,
        gtt_spill_detected=False,
        timestamp=1234567890.0,
    )
    assert state.vram_available_mb == 4096
    assert state.pacing_active is False
    assert state.schema_version == 2


def test_collect_telemetry_with_mock_sensors():
    """collect_telemetry uses sensors probe."""
    from cupola.sensors import SensorReading

    mock_reading = SensorReading(
        ts="2024-01-01T00:00:00+00:00",
        source="sysfs:card0",
        quality="ok",
        gpu_edge_c=75.0,
        gpu_hotspot_c=85.0,
        power_w=150.0,
        vram_used_bytes=8589934592,
        vram_total_bytes=16106127360,
    )

    with patch("cupola.telemetry.probe_sensors", return_value=mock_reading):
        state = collect_telemetry()

        assert state.hotspot_celsius == 85.0
        assert state.edge_celsius == 75.0
        assert state.power_w == 150.0
        # VRAM available = 15GB - 8GB = 7GB = 7168MB (16106127360 is 15GB, not 16)
        assert state.vram_available_mb == 7168
        # Headroom = 7168 - 2560 = 4608
        assert state.vram_headroom_mb == 4608


def test_pacing_active_high_temperature():
    """Pacing active when hotspot > 88°C."""
    from cupola.sensors import SensorReading

    mock_reading = SensorReading(
        ts="2024-01-01T00:00:00+00:00",
        source="sysfs:card0",
        quality="ok",
        gpu_edge_c=90.0,
        gpu_hotspot_c=95.0,
        power_w=150.0,
        vram_used_bytes=8589934592,
        vram_total_bytes=16106127360,
    )

    with patch("cupola.telemetry.probe_sensors", return_value=mock_reading):
        state = collect_telemetry()

        assert state.pacing_active is True
        assert state.hotspot_celsius == 95.0


def test_pacing_active_low_vram():
    """Pacing active when VRAM < 1.5GB."""
    from cupola.sensors import SensorReading

    mock_reading = SensorReading(
        ts="2024-01-01T00:00:00+00:00",
        source="sysfs:card0",
        quality="ok",
        gpu_edge_c=70.0,
        power_w=150.0,
        vram_used_bytes=14680064000,  # Only 1.4GB available of 16GB
        vram_total_bytes=16106127360,
    )

    with patch("cupola.telemetry.probe_sensors", return_value=mock_reading):
        state = collect_telemetry()

        assert state.pacing_active is True
        assert state.vram_available_mb < 1536


def test_energy_tier3_integration():
    """Energy computation via power integration (Tier 3)."""
    from cupola.sensors import SensorReading

    # 10 samples at 1 Hz, 100W each = 1000J = 0.0002778 kWh
    samples = []
    for i in range(10):
        samples.append(SensorReading(
            ts=f"2024-01-01T00:00:{i:02d}+00:00",
            source="sysfs:card0",
            quality="ok",
            power_w=100.0,
        ))

    result = compute_energy_tier3(samples, sample_hz=1.0)
    assert result.tier == 3
    assert result.energy_kwh is not None
    # 100W * 10s = 1000J = 0.0002778 kWh
    assert abs(result.energy_kwh - 0.0002778) < 0.00001


def test_energy_tier3_missing_samples():
    """Energy computation with missing power samples."""
    from cupola.sensors import SensorReading

    samples = []
    for i in range(5):
        if i == 2:
            # Missing power reading
            samples.append(SensorReading(
                ts=f"2024-01-01T00:00:{i:02d}+00:00",
                source="sysfs:card0",
                quality="degraded",
                power_w=None,
            ))
        else:
            samples.append(SensorReading(
                ts=f"2024-01-01T00:00:{i:02d}+00:00",
                source="sysfs:card0",
                quality="ok",
                power_w=100.0,
            ))

    result = compute_energy_tier3(samples, sample_hz=1.0)
    assert result.tier == 3
    assert result.missing_samples == 1
    assert result.quality == "degraded"


def test_sentinel_sampler_initialization():
    """SentinelSampler can be initialized."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)
        sampler = SentinelSampler(
            instance_uuid="test-uuid",
            profile="test",
            pid=1234,
            start_time=1234567890,
            state_dir=state_dir,
        )
        assert sampler.instance_uuid == "test-uuid"
        assert sampler.profile == "test"
        assert sampler.pid == 1234


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])
