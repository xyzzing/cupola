"""Sentinel sensor probe — AMD amdgpu sysfs/hwmon reader.

Reads generic sysfs paths for temperature, power, energy, fan, clocks, and
VRAM/GTT memory. No developer home paths. Missing files yield None and are
recorded in the missing list. Implausible values are treated as missing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class SensorReading:
    """One complete sensor probe snapshot."""

    ts: str
    source: str
    quality: str  # ok | degraded | unavailable
    gpu_edge_c: Optional[float] = None
    gpu_hotspot_c: Optional[float] = None
    gpu_mem_c: Optional[float] = None
    fan_pwm: Optional[int] = None
    fan_rpm: Optional[int] = None
    power_w: Optional[float] = None
    energy_uj: Optional[int] = None
    sclk_mhz: Optional[int] = None
    mclk_mhz: Optional[int] = None
    vram_used_bytes: Optional[int] = None
    vram_total_bytes: Optional[int] = None
    gtt_used_bytes: Optional[int] = None
    throttle_flags: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "source": self.source,
            "quality": self.quality,
            "gpu_edge_c": self.gpu_edge_c,
            "gpu_hotspot_c": self.gpu_hotspot_c,
            "gpu_mem_c": self.gpu_mem_c,
            "fan_pwm": self.fan_pwm,
            "fan_rpm": self.fan_rpm,
            "power_w": self.power_w,
            "energy_uj": self.energy_uj,
            "sclk_mhz": self.sclk_mhz,
            "mclk_mhz": self.mclk_mhz,
            "vram_used_bytes": self.vram_used_bytes,
            "vram_total_bytes": self.vram_total_bytes,
            "gtt_used_bytes": self.gtt_used_bytes,
            "throttle_flags": self.throttle_flags,
            "missing": self.missing,
        }


def _get_sysfs_root() -> Path:
    """Get sysfs root, honoring CUPOLA_SYSFS_ROOT for tests."""
    root = os.environ.get("CUPOLA_SYSFS_ROOT")
    if root:
        return Path(root)
    return Path("/sys")


def _read_int(path: Path) -> Optional[int]:
    """Read an integer value from a sysfs file."""
    try:
        with open(path, "r") as f:
            content = f.read().strip()
            if content:
                return int(content)
    except (OSError, ValueError):
        pass
    return None


def _read_float(path: Path) -> Optional[float]:
    """Read a float value from a sysfs file."""
    try:
        with open(path, "r") as f:
            content = f.read().strip()
            if content:
                return float(content)
    except (OSError, ValueError):
        pass
    return None


def _plausible_temperature(value: Optional[float]) -> Optional[float]:
    """Validate temperature is in plausible range (0-125°C)."""
    if value is None:
        return None
    if value < 0.0 or value > 125.0:
        return None
    return value


def _plausible_power(value: Optional[float]) -> Optional[float]:
    """Validate power is non-negative."""
    if value is None:
        return None
    if value < 0.0:
        return None
    return value


def probe_sensors() -> SensorReading:
    """Probe all available AMD GPU sensors via sysfs.

    Returns a SensorReading with quality:
    - 'ok' if all expected sensors are available
    - 'degraded' if some sensors are missing
    - 'unavailable' if no GPU sysfs is present
    """
    sysfs_root = _get_sysfs_root()
    ts = datetime.now(timezone.utc).isoformat()

    missing = []
    readings = {
        "gpu_edge_c": None,
        "gpu_hotspot_c": None,
        "gpu_mem_c": None,
        "fan_pwm": None,
        "fan_rpm": None,
        "power_w": None,
        "energy_uj": None,
        "sclk_mhz": None,
        "mclk_mhz": None,
        "vram_used_bytes": None,
        "vram_total_bytes": None,
        "gtt_used_bytes": None,
    }

    # Find AMD GPU card directories
    drm_dir = sysfs_root / "class" / "drm"
    card_dirs = []
    if drm_dir.exists():
        for entry in drm_dir.iterdir():
            if entry.is_dir() and entry.name.startswith("card"):
                card_dirs.append(entry)

    if not card_dirs:
        # No GPU found
        return SensorReading(
            ts=ts,
            source="sysfs:none",
            quality="unavailable",
            missing=["gpu"],
        )

    source = f"sysfs:{card_dirs[0].name}"

    for card in card_dirs:
        device = card / "device"
        if not device.exists():
            continue

        # Find hwmon directory — hwmon instances are under device/hwmon/hwmon*
        hwmon_parent = device / "hwmon"
        hwmon_dirs = []
        if hwmon_parent.exists() and hwmon_parent.is_dir():
            for entry in hwmon_parent.iterdir():
                if entry.name.startswith("hwmon") and entry.is_dir():
                    hwmon_dirs.append(entry)

        for hwmon in hwmon_dirs:
            # Temperature sensors
            for temp_file in hwmon.glob("temp*_input"):
                label_file = temp_file.with_name(temp_file.name.replace("_input", "_label"))
                label = ""
                if label_file.exists():
                    try:
                        with open(label_file, "r") as f:
                            label = f.read().strip().lower()
                    except OSError:
                        pass

                value_millideg = _read_int(temp_file)
                if value_millideg is not None:
                    value_c = value_millideg / 1000.0
                    value_c = _plausible_temperature(value_c)
                    if value_c is not None:
                        if "edge" in label:
                            readings["gpu_edge_c"] = value_c
                        elif "junction" in label or "hotspot" in label:
                            readings["gpu_hotspot_c"] = value_c
                        elif "mem" in label:
                            readings["gpu_mem_c"] = value_c
                        elif readings["gpu_edge_c"] is None:
                            # First temperature sensor — assume edge
                            readings["gpu_edge_c"] = value_c

            # Power average
            power_file = hwmon / "power1_average"
            if power_file.exists():
                value_uw = _read_int(power_file)
                if value_uw is not None:
                    value_w = value_uw / 1000000.0
                    readings["power_w"] = _plausible_power(value_w)

            # Energy input (cumulative µJ)
            energy_file = hwmon / "energy1_input"
            if energy_file.exists():
                readings["energy_uj"] = _read_int(energy_file)

            # Fan
            fan_input = hwmon / "fan1_input"
            if fan_input.exists():
                readings["fan_rpm"] = _read_int(fan_input)

            pwm_file = hwmon / "pwm1"
            if pwm_file.exists():
                readings["fan_pwm"] = _read_int(pwm_file)

        # VRAM/GTT memory info (not under hwmon)
        for mem_file in device.glob("mem_info_*"):
            name = mem_file.name
            value = _read_int(mem_file)
            if value is not None:
                if name == "mem_info_vram_used":
                    readings["vram_used_bytes"] = value
                elif name == "mem_info_vram_total":
                    readings["vram_total_bytes"] = value
                elif name == "mem_info_gtt_used":
                    readings["gtt_used_bytes"] = value

        # Clocks (not always available)
        sclk_file = device / "pp_od_clk_voltage"
        if sclk_file.exists():
            try:
                with open(sclk_file, "r") as f:
                    content = f.read()
                    # Parse current SCLK from output
                    for line in content.split("\n"):
                        if "Current" in line and "SCLK" in line:
                            parts = line.split()
                            for i, part in enumerate(parts):
                                if "MHz" in part and i > 0:
                                    try:
                                        readings["sclk_mhz"] = int(parts[i - 1])
                                    except (IndexError, ValueError):
                                        pass
            except OSError:
                pass

    # Determine quality and missing sensors
    expected_sensors = [
        ("gpu_edge_c", "temperature"),
        ("power_w", "power"),
    ]

    for key, name in expected_sensors:
        if readings[key] is None:
            missing.append(name)

    if readings["gpu_hotspot_c"] is None:
        missing.append("hotspot")
    if readings["fan_rpm"] is None:
        missing.append("fan")
    if readings["vram_used_bytes"] is None:
        missing.append("vram")

    # Quality assessment
    if readings["gpu_edge_c"] is None and readings["gpu_hotspot_c"] is None:
        quality = "degraded"
    elif not missing:
        quality = "ok"
    else:
        quality = "degraded"

    return SensorReading(
        ts=ts,
        source=source,
        quality=quality,
        gpu_edge_c=readings["gpu_edge_c"],
        gpu_hotspot_c=readings["gpu_hotspot_c"],
        gpu_mem_c=readings["gpu_mem_c"],
        fan_pwm=readings["fan_pwm"],
        fan_rpm=readings["fan_rpm"],
        power_w=readings["power_w"],
        energy_uj=readings["energy_uj"],
        sclk_mhz=readings["sclk_mhz"],
        mclk_mhz=readings["mclk_mhz"],
        vram_used_bytes=readings["vram_used_bytes"],
        vram_total_bytes=readings["vram_total_bytes"],
        gtt_used_bytes=readings["gtt_used_bytes"],
        missing=missing,
    )
