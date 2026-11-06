"""Tests for Sentinel sensor probe with fake sysfs fixtures."""

import os
from pathlib import Path

from cupola.sensors import SensorReading, probe_sensors


def _write_file(path: Path, content: str) -> None:
    """Write content to a file, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _create_full_hwmon_tree(tmp_path: Path, bdf: str = "0000:03:00.0") -> Path:
    """Create a complete fake sysfs tree with all sensors.

    The card's ``device`` node is symlinked to a PCI device directory laid out
    like the real one, because that symlink — not a path convention — is what
    the probe uses to reach link state and AER counters.
    """
    sysfs = tmp_path / "sys"
    card = sysfs / "class" / "drm" / "card0" / "device"
    hwmon = card / "hwmon" / "hwmon0"
    pci_dir = sysfs / "bus" / "pci" / "devices" / bdf

    # ``device`` is a symlink into the PCI tree, so it is created before
    # anything is written through it: writing files under ``card`` would
    # otherwise land them in the PCI directory.
    pci_dir.mkdir(parents=True)
    card.parent.mkdir(parents=True, exist_ok=True)
    card.symlink_to(pci_dir, target_is_directory=True)

    # Temperature sensors
    _write_file(hwmon / "temp1_input", "75000")  # 75°C edge
    _write_file(hwmon / "temp1_label", "edge")
    _write_file(hwmon / "temp2_input", "85000")  # 85°C hotspot
    _write_file(hwmon / "temp2_label", "junction")
    _write_file(hwmon / "temp2_crit", "110000")  # 110°C hard limit
    _write_file(hwmon / "temp3_input", "60000")  # 60°C memory
    _write_file(hwmon / "temp3_label", "mem")

    # Power
    _write_file(hwmon / "power1_average", "150000000")  # 150W in µW
    _write_file(hwmon / "power1_cap", "300000000")  # 300W cap

    # Energy (cumulative µJ)
    _write_file(hwmon / "energy1_input", "5400000000")  # 5.4 J

    # Fan
    _write_file(hwmon / "fan1_input", "2500")  # 2500 RPM
    _write_file(hwmon / "pwm1", "128")  # 50% PWM

    # Clocks, keyed by label rather than index. The healthy fixture runs close
    # to the DPM ceiling so the throttle flags stay empty.
    _write_file(hwmon / "freq1_input", "2400000000")
    _write_file(hwmon / "freq1_label", "sclk")
    _write_file(hwmon / "freq2_input", "1250000000")
    _write_file(hwmon / "freq2_label", "mclk")

    # VRAM/GTT
    _write_file(card / "mem_info_vram_used", "8589934592")  # 8 GB
    _write_file(card / "mem_info_vram_total", "16106127360")  # 16 GB
    _write_file(card / "mem_info_gtt_used", "1073741824")  # 1 GB
    _write_file(card / "mem_info_gtt_total", "34359738368")  # 32 GB

    # Engine busy percent
    _write_file(card / "gpu_busy_percent", "84")
    _write_file(card / "mem_busy_percent", "19")

    # DPM ceiling: the comparison that turns a clock reading into throttle
    # evidence.
    _write_file(card / "pp_dpm_sclk", "0: 500Mhz \n1: 2250Mhz *\n2: 2526Mhz\n")

    # PCI device directory, reached through the symlinked ``device`` node.
    _write_file(pci_dir / "current_link_speed", "16.0 GT/s PCIe")
    _write_file(pci_dir / "max_link_speed", "16.0 GT/s PCIe")
    _write_file(pci_dir / "current_link_width", "16")
    _write_file(pci_dir / "max_link_width", "16")
    # AER nodes are per-error tables, not scalars; only TOTAL_ERR_* is an
    # aggregate worth exporting.
    _write_file(pci_dir / "aer_dev_correctable", "RxErr 0\nBadTLP 0\nTOTAL_ERR_COR 0\n")
    _write_file(pci_dir / "aer_dev_fatal", "DLP 0\nTLP 0\nTOTAL_ERR_FATAL 0\n")
    _write_file(pci_dir / "aer_dev_nonfatal", "UncorrErr 0\nTOTAL_ERR_NONFATAL 0\n")

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

    def test_throttle_relevant_sensors(self, tmp_path):
        """Busy %, labelled clocks, DPM ceiling, power cap and link state read."""
        sysfs = _create_full_hwmon_tree(tmp_path)
        os.environ["CUPOLA_SYSFS_ROOT"] = str(sysfs)

        try:
            reading = probe_sensors()

            assert reading.gpu_busy_pct == 84
            assert reading.mem_busy_pct == 19
            # Label-keyed clocks: this card has no pp_od_clk_voltage, so the
            # old parse silently produced None and no throttle signal at all.
            assert reading.sclk_mhz == 2400
            assert reading.mclk_mhz == 1250
            assert reading.sclk_max_mhz == 2526
            assert reading.power_cap_w == 300.0
            assert reading.hotspot_crit_c == 110.0
            assert reading.gtt_total_bytes == 34359738368
            assert reading.pcie_current_speed_gts == 16.0
            assert reading.pcie_max_speed_gts == 16.0
            assert reading.pcie_current_width == 16
            assert reading.pcie_max_width == 16
            assert reading.aer_correctable == 0
            assert reading.throttle_flags == []
        finally:
            del os.environ["CUPOLA_SYSFS_ROOT"]

    def test_clock_below_ceiling_while_busy_is_flagged(self, tmp_path):
        """A busy engine holding a clock far under its ceiling is flagged."""
        sysfs = _create_full_hwmon_tree(tmp_path)
        card = sysfs / "class" / "drm" / "card0" / "device"
        _write_file(card / "hwmon" / "hwmon0" / "freq1_input", "900000000")  # 900 MHz
        os.environ["CUPOLA_SYSFS_ROOT"] = str(sysfs)

        try:
            reading = probe_sensors()

            assert reading.gpu_busy_pct == 84
            assert reading.sclk_mhz == 900
            assert "clock_below_ceiling" in reading.throttle_flags
        finally:
            del os.environ["CUPOLA_SYSFS_ROOT"]

    def test_dpm_ceiling_is_a_static_state_not_the_live_one(self, tmp_path):
        """``pp_dpm_sclk`` marks the current state with ``*``, and on RDNA3 that
        state moves every sample.

        Taking the maximum of the whole table would set the ceiling to the
        reading it is compared against: the ratio is then always 1, the
        dashboard draws the ceiling on top of the clock, and a card held back
        below a higher ceiling is never flagged.
        """
        sysfs = _create_full_hwmon_tree(tmp_path)
        card = sysfs / "class" / "drm" / "card0" / "device"
        _write_file(card / "pp_dpm_sclk", "0: 500Mhz \n1: 3054Mhz *\n2: 2526Mhz\n")
        _write_file(card / "hwmon" / "hwmon0" / "freq1_input", "3054000000")
        os.environ["CUPOLA_SYSFS_ROOT"] = str(sysfs)

        try:
            reading = probe_sensors()

            assert reading.sclk_mhz == 3054
            assert reading.sclk_max_mhz == 2526
            # Running above every static state is boost, not a limit.
            assert "clock_below_ceiling" not in reading.throttle_flags
        finally:
            del os.environ["CUPOLA_SYSFS_ROOT"]

    def test_dpm_table_with_only_a_live_state_gives_no_ceiling_gap(self, tmp_path):
        """One state, marked live: the table is a floor, not a ceiling.

        There is no evidence of a higher state, so nothing may be flagged from
        it — a card that reports only what it is doing now is not being called
        throttled for doing it.
        """
        sysfs = _create_full_hwmon_tree(tmp_path)
        card = sysfs / "class" / "drm" / "card0" / "device"
        _write_file(card / "pp_dpm_sclk", "0: 1200Mhz *\n")
        _write_file(card / "hwmon" / "hwmon0" / "freq1_input", "1200000000")
        os.environ["CUPOLA_SYSFS_ROOT"] = str(sysfs)

        try:
            reading = probe_sensors()

            assert reading.sclk_max_mhz == 1200
            assert "clock_below_ceiling" not in reading.throttle_flags
        finally:
            del os.environ["CUPOLA_SYSFS_ROOT"]

    def test_idle_low_clock_is_not_throttle(self, tmp_path):
        """A low clock with an idle engine is idleness, not a limit."""
        sysfs = _create_full_hwmon_tree(tmp_path)
        card = sysfs / "class" / "drm" / "card0" / "device"
        _write_file(card / "gpu_busy_percent", "3")
        _write_file(card / "hwmon" / "hwmon0" / "freq1_input", "500000000")
        os.environ["CUPOLA_SYSFS_ROOT"] = str(sysfs)

        try:
            reading = probe_sensors()

            assert reading.sclk_mhz == 500
            assert "clock_below_ceiling" not in reading.throttle_flags
        finally:
            del os.environ["CUPOLA_SYSFS_ROOT"]

    def test_downgraded_pcie_link_is_flagged(self, tmp_path):
        """A link that negotiated below its maximum is a data-path throttle."""
        sysfs = _create_full_hwmon_tree(tmp_path)
        pci_dir = sysfs / "bus" / "pci" / "devices" / "0000:03:00.0"
        _write_file(pci_dir / "current_link_speed", "8.0 GT/s PCIe")
        _write_file(pci_dir / "current_link_width", "4")
        os.environ["CUPOLA_SYSFS_ROOT"] = str(sysfs)

        try:
            reading = probe_sensors()

            assert reading.pcie_current_speed_gts == 8.0
            assert reading.pcie_current_width == 4
            assert "pcie_link_downgraded" in reading.throttle_flags
        finally:
            del os.environ["CUPOLA_SYSFS_ROOT"]

    def test_power_at_cap_is_flagged(self, tmp_path):
        """Power pinned against the cap is flagged as a power limit."""
        sysfs = _create_full_hwmon_tree(tmp_path)
        hwmon = sysfs / "class" / "drm" / "card0" / "device" / "hwmon" / "hwmon0"
        _write_file(hwmon / "power1_average", "299000000")  # 299 W of a 300 W cap
        os.environ["CUPOLA_SYSFS_ROOT"] = str(sysfs)

        try:
            reading = probe_sensors()

            assert "power_at_cap" in reading.throttle_flags
        finally:
            del os.environ["CUPOLA_SYSFS_ROOT"]

    def test_uncorrectable_aer_is_flagged(self, tmp_path):
        """Uncorrectable PCIe errors are reported, not left as a bare number."""
        sysfs = _create_full_hwmon_tree(tmp_path)
        pci_dir = sysfs / "bus" / "pci" / "devices" / "0000:03:00.0"
        _write_file(pci_dir / "aer_dev_nonfatal",
                      "UncorrErr 3\nTOTAL_ERR_NONFATAL 3\n")
        os.environ["CUPOLA_SYSFS_ROOT"] = str(sysfs)

        try:
            reading = probe_sensors()

            assert reading.aer_nonfatal == 3
            assert "pcie_aer_uncorrectable" in reading.throttle_flags
        finally:
            del os.environ["CUPOLA_SYSFS_ROOT"]

    def test_aer_totals_are_read_from_the_line_not_the_file(self, tmp_path):
        """A table holding several aggregates must not misattribute the first.

        Real sysfs puts one ``TOTAL_ERR_*`` row per node, but the parse keys on
        the matched line: a table that carries more than one used to have its
        first row charged to whichever file it happened to sit in, which read a
        non-fatal count as zero.
        """
        sysfs = _create_full_hwmon_tree(tmp_path)
        pci_dir = sysfs / "bus" / "pci" / "devices" / "0000:03:00.0"
        _write_file(pci_dir / "aer_dev_nonfatal",
                    "ROOT_PORT: 0000:02:00.0\n"
                    "DEV: 0000:03:00.0\n"
                    "UncorrErr 3\n"
                    "TOTAL_ERR_COR  0\n"
                    "TOTAL_ERR_NONFATAL  3\n"
                    "TOTAL_ERR_FATAL  1\n")
        os.environ["CUPOLA_SYSFS_ROOT"] = str(sysfs)

        try:
            reading = probe_sensors()

            assert reading.aer_correctable == 0
            assert reading.aer_nonfatal == 3
            assert reading.aer_fatal == 1
            assert "pcie_aer_uncorrectable" in reading.throttle_flags
        finally:
            del os.environ["CUPOLA_SYSFS_ROOT"]

    def test_missing_throttle_sensors_are_named(self, tmp_path):
        """Absent busy/clock/cap/link nodes are reported missing, never zero."""
        sysfs = _create_partial_hwmon_tree(tmp_path)
        os.environ["CUPOLA_SYSFS_ROOT"] = str(sysfs)

        try:
            reading = probe_sensors()

            assert reading.gpu_busy_pct is None
            assert reading.sclk_mhz is None
            assert reading.power_cap_w is None
            assert reading.pcie_current_width is None
            for name in ("busy", "clock", "power_cap", "pcie_link", "aer"):
                assert name in reading.missing
            assert reading.throttle_flags == []
        finally:
            del os.environ["CUPOLA_SYSFS_ROOT"]

    def test_connector_dirs_are_not_cards(self, tmp_path):
        """Connector entries are outputs, not cards, and must not shadow one.

        ``/sys/class/drm`` holds ``card1-HDMI-A-1`` style entries next to the
        card itself. They have no DPM table, so counting them as cards let the
        last one visited erase the clock ceiling — and made the reported source
        depend on directory order.
        """
        sysfs = _create_full_hwmon_tree(tmp_path)
        drm = sysfs / "class" / "drm"
        pci_dir = sysfs / "bus" / "pci" / "devices" / "0000:03:00.0"
        for connector in ("card0-HDMI-A-1", "card0-DP-2", "card0-Writeback-1"):
            (drm / connector).mkdir()
            (drm / connector / "device").symlink_to(pci_dir, target_is_directory=True)
        os.environ["CUPOLA_SYSFS_ROOT"] = str(sysfs)

        try:
            reading = probe_sensors()

            assert reading.source == "sysfs:card0"
            assert reading.sclk_max_mhz == 2526
            assert reading.throttle_flags == []
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
