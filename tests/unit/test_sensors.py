"""Tests for Sentinel sensor probe with fake sysfs fixtures."""

import os
from pathlib import Path

from cupola.sensors import SensorReading, probe_sensors


def _write_file(path: Path, content: str) -> None:
    """Write content to a file, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _create_full_hwmon_tree(tmp_path: Path) -> Path:
    """Create a complete fake sysfs tree with all sensors."""
    sysfs = tmp_path / "sys"
    card = sysfs / "class" / "drm" / "card0" / "device"
    hwmon = card / "hwmon" / "hwmon0"

    # Temperature sensors
    _write_file(hwmon / "temp1_input", "75000")  # 75°C edge
    _write_file(hwmon / "temp1_label", "edge")
    _write_file(hwmon / "temp2_input", "85000")  # 85°C hotspot
    _write_file(hwmon / "temp2_label", "junction")
    _write_file(hwmon / "temp3_input", "60000")  # 60°C memory
    _write_file(hwmon / "temp3_label", "mem")

    # Power
    _write_file(hwmon / "power1_average", "150000000")  # 150W in µW

    # Energy (cumulative µJ)
    _write_file(hwmon / "energy1_input", "5400000000")  # 5.4 J

    # Fan
    _write_file(hwmon / "fan1_input", "2500")  # 2500 RPM
    _write_file(hwmon / "pwm1", "128")  # 50% PWM

    # VRAM/GTT
    _write_file(card / "mem_info_vram_used", "8589934592")  # 8 GB
    _write_file(card / "mem_info_vram_total", "16106127360")  # 16 GB
    _write_file(card / "mem_info_gtt_used", "1073741824")  # 1 GB

    return sysfs


def _create_partial_hwmon_tree(tmp_path: Path) -> Path:
    """Create a sysfs tree with missing power sensor."""
    sysfs = tmp_path / "sys"
    card = sysfs / "class" / "drm" / "card0" / "device"
    hwmon = card / "hwmon" / "hwmon0"

    # Only temperatures, no power
    _write_file(hwmon / "temp1_input", "70000")  # 70°C
    _write_file(hwmon / "temp1_label", "edge")

    return sysfs


def _create_no_gpu_tree(tmp_path: Path) -> Path:
    """Create a sysfs tree with no GPU."""
    sysfs = tmp_path / "sys"
    # Empty class/drm directory
    (sysfs / "class" / "drm").mkdir(parents=True, exist_ok=True)
    return sysfs


def _create_implausible_temp_tree(tmp_path: Path) -> Path:
    """Create a sysfs tree with implausible temperature."""
    sysfs = tmp_path / "sys"
    card = sysfs / "class" / "drm" / "card0" / "device"
    hwmon = card / "hwmon" / "hwmon0"

    # 200°C — implausible
    _write_file(hwmon / "temp1_input", "200000")
    _write_file(hwmon / "temp1_label", "edge")

    return sysfs


class TestSensorProbe:
    """Test suite for sensor probe."""

    def test_full_sensors_ok(self, tmp_path):
        """Test complete sensor tree returns quality='ok'."""
        sysfs = _create_full_hwmon_tree(tmp_path)
        os.environ["CUPOLA_SYSFS_ROOT"] = str(sysfs)

        try:
            reading = probe_sensors()

            assert reading.quality == "ok"
            assert reading.gpu_edge_c == 75.0
            assert reading.gpu_hotspot_c == 85.0
            assert reading.gpu_mem_c == 60.0
            assert reading.power_w == 150.0
            assert reading.energy_uj == 5400000000
            assert reading.fan_rpm == 2500
            assert reading.fan_pwm == 128
            assert reading.vram_used_bytes == 8589934592
            assert reading.vram_total_bytes == 16106127360
            assert reading.gtt_used_bytes == 1073741824
            assert "power" not in reading.missing
        finally:
            del os.environ["CUPOLA_SYSFS_ROOT"]

    def test_missing_power_degraded(self, tmp_path):
        """Test missing power sensor returns quality='degraded'."""
        sysfs = _create_partial_hwmon_tree(tmp_path)
        os.environ["CUPOLA_SYSFS_ROOT"] = str(sysfs)

        try:
            reading = probe_sensors()

            assert reading.quality == "degraded"
            assert reading.gpu_edge_c == 70.0
            assert reading.power_w is None
            assert "power" in reading.missing
        finally:
            del os.environ["CUPOLA_SYSFS_ROOT"]

    def test_no_gpu_unavailable(self, tmp_path):
        """Test no GPU returns quality='unavailable'."""
        sysfs = _create_no_gpu_tree(tmp_path)
        os.environ["CUPOLA_SYSFS_ROOT"] = str(sysfs)

        try:
            reading = probe_sensors()

            assert reading.quality == "unavailable"
            assert "gpu" in reading.missing
        finally:
            del os.environ["CUPOLA_SYSFS_ROOT"]

    def test_implausible_temperature_treated_as_missing(self, tmp_path):
        """Test implausible temperature (200°C) is treated as missing."""
        sysfs = _create_implausible_temp_tree(tmp_path)
        os.environ["CUPOLA_SYSFS_ROOT"] = str(sysfs)

        try:
            reading = probe_sensors()

            # 200°C is implausible, so edge temp should be None
            assert reading.gpu_edge_c is None
            assert "temperature" in reading.missing
        finally:
            del os.environ["CUPOLA_SYSFS_ROOT"]

    def test_sensor_reading_to_dict(self):
        """Test SensorReading serialization."""
        reading = SensorReading(
            ts="2024-01-01T00:00:00+00:00",
            source="sysfs:card0",
            quality="ok",
            gpu_edge_c=75.0,
            power_w=150.0,
        )

        data = reading.to_dict()
        assert data["ts"] == "2024-01-01T00:00:00+00:00"
        assert data["source"] == "sysfs:card0"
        assert data["quality"] == "ok"
        assert data["gpu_edge_c"] == 75.0
        assert data["power_w"] == 150.0
        assert data["missing"] == []

    def test_default_sysfs_root(self):
        """Test default sysfs root is /sys when CUPOLA_SYSFS_ROOT not set."""
        if "CUPOLA_SYSFS_ROOT" in os.environ:
            del os.environ["CUPOLA_SYSFS_ROOT"]

        # Should not raise, but likely returns unavailable on CI
        reading = probe_sensors()
        assert isinstance(reading, SensorReading)
