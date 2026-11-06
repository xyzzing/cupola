"""Sentinel sensor probe — AMD amdgpu sysfs/hwmon reader.

Reads generic sysfs paths for temperature, power, energy, fan, clocks,
VRAM/GTT memory, engine busy percent, PCIe link state and AER error counters.
No developer home paths. Missing files yield None and are recorded in the
missing list. Implausible values are treated as missing.

The PCIe and busy-percent nodes are what make silent throttling visible on
hardware that exposes no throttle-reason accumulators: a link that negotiated
below its maximum, or a clock ceiling far below the DPM table while the engine
is busy, are the observable shadows of a limit that the driver will not name.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

#: PCI device directory name, e.g. ``0000:03:00.0``. The DRM ``device`` node is
#: a symlink to it, so the name is what maps a card back to its PCI node.
_CARD_RE = re.compile(r"card[0-9]+")
_BDF_RE = re.compile(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]")

#: ``current_link_speed`` reads like ``16.0 GT/s PCIe``.
_LINK_SPEED_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*GT/s", re.IGNORECASE)

#: A DPM clock line reads like ``1: 2250Mhz *``; the ``*`` marks the live state.
_DPM_STATE_RE = re.compile(r"^\s*\d+:\s*([0-9]+)\s*[Mm][Hh]z[ \t]*(\*?)[ \t]*$",
                           re.MULTILINE)

#: Fraction of the DPM ceiling that still counts as "running at the ceiling".
#: Below this, with the engine busy, something is limiting the clock.
CLOCK_CEILING_FRACTION = 0.85

#: Fraction of the power cap that counts as pinned against the cap.
POWER_CAP_FRACTION = 0.97

#: Engine busy percent above which a low clock is a throttle, not idleness.
BUSY_MIN_PCT = 80


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
    gtt_total_bytes: Optional[int] = None
    gpu_busy_pct: Optional[int] = None
    mem_busy_pct: Optional[int] = None
    power_cap_w: Optional[float] = None
    hotspot_crit_c: Optional[float] = None
    sclk_max_mhz: Optional[int] = None
    pcie_current_speed_gts: Optional[float] = None
    pcie_max_speed_gts: Optional[float] = None
    pcie_current_width: Optional[int] = None
    pcie_max_width: Optional[int] = None
    aer_correctable: Optional[int] = None
    aer_fatal: Optional[int] = None
    aer_nonfatal: Optional[int] = None
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
            "sclk_max_mhz": self.sclk_max_mhz,
            "mclk_mhz": self.mclk_mhz,
            "vram_used_bytes": self.vram_used_bytes,
            "vram_total_bytes": self.vram_total_bytes,
            "gtt_used_bytes": self.gtt_used_bytes,
            "gtt_total_bytes": self.gtt_total_bytes,
            "gpu_busy_pct": self.gpu_busy_pct,
            "mem_busy_pct": self.mem_busy_pct,
            "power_cap_w": self.power_cap_w,
            "hotspot_crit_c": self.hotspot_crit_c,
            "pcie_current_speed_gts": self.pcie_current_speed_gts,
            "pcie_max_speed_gts": self.pcie_max_speed_gts,
            "pcie_current_width": self.pcie_current_width,
            "pcie_max_width": self.pcie_max_width,
            "aer_correctable": self.aer_correctable,
            "aer_fatal": self.aer_fatal,
            "aer_nonfatal": self.aer_nonfatal,
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


def _plausible_pct(value: Optional[int]) -> Optional[int]:
    """Validate a busy percentage is 0-100."""
    if value is None:
        return None
    if value < 0 or value > 100:
        return None
    return value


def _plausible_counter(value: Optional[int]) -> Optional[int]:
    """Validate a cumulative counter is non-negative.

    PCIe error counters are read as ``%lld`` and a driver bug or a rollover can
    surface a negative value. A negative count is worse than no count: it would
    read as a healthy link that has never errored.
    """
    if value is None or value < 0:
        return None
    return value


def _parse_link_speed(text: Optional[str]) -> Optional[float]:
    """Parse ``16.0 GT/s PCIe`` into ``16.0``; anything else is missing."""
    if not text:
        return None
    match = _LINK_SPEED_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _read_text(path: Path) -> Optional[str]:
    """Read a whole sysfs text node (multi-line nodes included)."""
    try:
        with open(path, "r") as f:
            content = f.read().strip()
            return content or None
    except OSError:
        return None


def _pci_device_dir(sysfs_root: Path, device: Path) -> Optional[Path]:
    """Map a DRM ``device`` node to its PCI device directory.

    ``/sys/class/drm/card*/device`` is a symlink into the PCI tree, so the
    resolved directory's name is the BDF. A fixture tree that is not laid out
    that way yields None, and the PCIe fields are reported missing rather than
    guessed at.
    """
    try:
        name = device.resolve().name
    except OSError:
        return None
    if not _BDF_RE.fullmatch(name):
        return None
    pci_dir = sysfs_root / "bus" / "pci" / "devices" / name
    return pci_dir if pci_dir.is_dir() else None


def _read_max_dpm_clock(device: Path) -> Optional[int]:
    """Highest static SCLK state in ``pp_dpm_sclk``.

    This is the ceiling the GPU *could* reach. Compared against the clock it
    actually holds while busy, it is the only throttle evidence a consumer
    RDNA3 part offers; there is no throttle-reason accumulator to read.

    The state marked ``*`` is the live clock, not a ceiling: on this hardware it
    changes every sample. Taking the maximum of the whole table would set the
    ceiling to the reading it is meant to be compared against — the dashboard
    then draws the ceiling on top of the clock and a held-back card looks
    normal. Only the unmarked states are evidence of a ceiling.
    """
    content = _read_text(device / "pp_dpm_sclk")
    if content is None:
        return None
    states = _DPM_STATE_RE.findall(content)
    if not states:
        return None
    static = [int(clock) for clock, active in states if not active]
    if not static:
        # Every state is marked live, or there is only one state: the card is
        # already running at the only clock the table offers, so the table is a
        # floor rather than a ceiling and the comparison can only loosen.
        static = [int(clock) for clock, _ in states]
    return max(static)


_AER_TOTAL_RE = re.compile(r"^TOTAL_ERR_(COR|FATAL|NONFATAL)\s+(-?[0-9]+)\s*$",
                           re.MULTILINE)


def _read_aer_totals(pci_dir: Path) -> dict:
    """Total correctable / fatal / non-fatal PCIe errors.

    These nodes are not scalars: each is a table of per-error counters. The
    ``TOTAL_ERR_*`` line is the aggregate, and it is the only thing worth
    exporting — the individual rows are diagnostic detail, not a health signal.
    """
    totals = {"aer_correctable": None, "aer_fatal": None, "aer_nonfatal": None}
    key_for = {"COR": "aer_correctable", "FATAL": "aer_fatal", "NONFATAL": "aer_nonfatal"}
    for node in ("aer_dev_correctable", "aer_dev_fatal", "aer_dev_nonfatal"):
        content = _read_text(pci_dir / node)
        if content is None:
            continue
        # Key off the matched line, never the file name. A node normally holds
        # one aggregate, but a table that carries several must not have its
        # first row attributed to whatever file it happened to sit in.
        for match in _AER_TOTAL_RE.finditer(content):
            totals[key_for[match.group(1)]] = _plausible_counter(int(match.group(2)))
    return totals


def _derive_throttle_flags(readings: dict) -> list[str]:
    """Name the limit the readings imply.

    These are inferences, not driver-reported reasons, and are labelled as
    flags rather than causes. ``clock_below_ceiling`` fires when a busy engine
    holds a clock well under its DPM ceiling — the shape of thermal or power
    limiting — and says nothing about which one.
    """
    flags: list[str] = []

    cur_speed = readings["pcie_current_speed_gts"]
    max_speed = readings["pcie_max_speed_gts"]
    cur_width = readings["pcie_current_width"]
    max_width = readings["pcie_max_width"]
    if (cur_speed is not None and max_speed is not None and cur_speed < max_speed) or (
        cur_width is not None and max_width is not None and cur_width < max_width
    ):
        flags.append("pcie_link_downgraded")

    if (readings["aer_fatal"] or 0) > 0 or (readings["aer_nonfatal"] or 0) > 0:
        flags.append("pcie_aer_uncorrectable")

    power, cap = readings["power_w"], readings["power_cap_w"]
    if power is not None and cap is not None and cap > 0 and power >= POWER_CAP_FRACTION * cap:
        flags.append("power_at_cap")

    sclk, sclk_max = readings["sclk_mhz"], readings["sclk_max_mhz"]
    busy = readings["gpu_busy_pct"]
    if (sclk is not None and sclk_max and busy is not None
            and busy >= BUSY_MIN_PCT and sclk < CLOCK_CEILING_FRACTION * sclk_max):
        flags.append("clock_below_ceiling")

    hotspot = readings["gpu_hotspot_c"]
    hotspot_crit = readings["hotspot_crit_c"]
    if hotspot is not None and hotspot_crit is not None and hotspot >= hotspot_crit:
        flags.append("hotspot_at_crit")

    return flags


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
        "power_cap_w": None,
        "energy_uj": None,
        "sclk_mhz": None,
        "sclk_max_mhz": None,
        "mclk_mhz": None,
        "vram_used_bytes": None,
        "vram_total_bytes": None,
        "gtt_used_bytes": None,
        "gtt_total_bytes": None,
        "gpu_busy_pct": None,
        "mem_busy_pct": None,
        "hotspot_crit_c": None,
        "pcie_current_speed_gts": None,
        "pcie_max_speed_gts": None,
        "pcie_current_width": None,
        "pcie_max_width": None,
        "aer_correctable": None,
        "aer_fatal": None,
        "aer_nonfatal": None,
    }

    # Find AMD GPU card directories. Only ``card<digits>`` is a card: the
    # ``card1-HDMI-A-1`` style entries are connectors, and treating them as
    # cards means reading the same GPU several times, with the last one to be
    # visited winning. ``iterdir`` order is not stable, so that also made the
    # reported source nondeterministic.
    drm_dir = sysfs_root / "class" / "drm"
    card_dirs = []
    if drm_dir.exists():
        for entry in drm_dir.iterdir():
            if entry.is_dir() and _CARD_RE.fullmatch(entry.name):
                card_dirs.append(entry)
    card_dirs.sort(key=lambda p: p.name)

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
            for temp_file in sorted(hwmon.glob("temp*_input")):
                label_file = temp_file.with_name(temp_file.name.replace("_input", "_label"))
                label = ""
                if label_file.exists():
                    try:
                        with open(label_file, "r") as f:
                            label = f.read().strip().lower()
                    except OSError:
                        pass

                value_millideg = _read_int(temp_file)
                if value_millideg is None:
                    continue
                value_c = _plausible_temperature(value_millideg / 1000.0)
                if value_c is None:
                    continue

                # The critical limit belongs to the same sensor index, and is
                # what turns a temperature into headroom against a hard stop.
                index = temp_file.name.split("_")[0][len("temp"):]
                crit_file = hwmon / f"temp{index}_crit"

                if "edge" in label:
                    readings["gpu_edge_c"] = value_c
                elif "junction" in label or "hotspot" in label:
                    readings["gpu_hotspot_c"] = value_c
                    crit_millideg = _read_int(crit_file)
                    if crit_millideg is not None:
                        readings["hotspot_crit_c"] = crit_millideg / 1000.0
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

            # Power cap: the ceiling power1_average is being held under.
            cap_file = hwmon / "power1_cap"
            if cap_file.exists():
                value_uw = _read_int(cap_file)
                if value_uw is not None:
                    readings["power_cap_w"] = _plausible_power(value_uw / 1000000.0)

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

            # Clocks, keyed by label so the index order cannot be guessed.
            # ``freq1_input``/``freq2_input`` labelled ``sclk``/``mclk`` are the
            # nodes this driver actually populates; ``pp_od_clk_voltage`` is
            # absent on Navi 31, so relying on it silently loses the clock —
            # and without the clock there is no throttle signal at all.
            for freq_file in sorted(hwmon.glob("freq*_input")):
                index = freq_file.name.split("_")[0][len("freq"):]
                label_file = hwmon / f"freq{index}_label"
                label = (_read_text(label_file) or "").lower()
                value_hz = _read_int(freq_file)
                if value_hz is None or value_hz <= 0:
                    continue
                if label == "sclk" and readings["sclk_mhz"] is None:
                    readings["sclk_mhz"] = value_hz // 1_000_000
                elif label == "mclk" and readings["mclk_mhz"] is None:
                    readings["mclk_mhz"] = value_hz // 1_000_000

        # Engine busy percent — the closest available stand-in for occupancy
        # on hardware that exposes no CU-occupancy counter.
        busy_file = device / "gpu_busy_percent"
        if busy_file.exists():
            readings["gpu_busy_pct"] = _plausible_pct(_read_int(busy_file))
        mem_busy_file = device / "mem_busy_percent"
        if mem_busy_file.exists():
            readings["mem_busy_pct"] = _plausible_pct(_read_int(mem_busy_file))

        # VRAM/GTT memory info (not under hwmon)
        for mem_file in device.glob("mem_info_*"):
            name = mem_file.name
            value = _read_int(mem_file)
            if value is None:
                continue
            if name == "mem_info_vram_used":
                readings["vram_used_bytes"] = value
            elif name == "mem_info_vram_total":
                readings["vram_total_bytes"] = value
            elif name == "mem_info_gtt_used":
                readings["gtt_used_bytes"] = value
            elif name == "mem_info_gtt_total":
                readings["gtt_total_bytes"] = value

        # PCIe link state and AER counters, reached through the PCI node the
        # DRM device symlink resolves to. Byte counters (TX/RX) are not
        # exposed by this driver, so the negotiated link is what is measurable.
        # First card wins: this build covers a single GPU, and a second card
        # silently overwriting the first would misattribute one device's link
        # to the source named above.
        pci_dir = _pci_device_dir(sysfs_root, device)
        if pci_dir is not None:
            pci_values = {
                "pcie_current_speed_gts": _parse_link_speed(
                    _read_text(pci_dir / "current_link_speed")),
                "pcie_max_speed_gts": _parse_link_speed(
                    _read_text(pci_dir / "max_link_speed")),
                "pcie_current_width": _read_int(pci_dir / "current_link_width"),
                "pcie_max_width": _read_int(pci_dir / "max_link_width"),
            }
            pci_values.update(_read_aer_totals(pci_dir))
            for key, value in pci_values.items():
                if readings[key] is None:
                    readings[key] = value

        # Clock ceiling from the DPM table: the comparison that makes a low
        # clock mean something.
        if readings["sclk_max_mhz"] is None:
            readings["sclk_max_mhz"] = _read_max_dpm_clock(device)

        # Older powerplay interface, kept as a fallback only.
        if readings["sclk_mhz"] is None:
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
    if readings["gpu_busy_pct"] is None:
        missing.append("busy")
    if readings["sclk_mhz"] is None:
        missing.append("clock")
    if readings["power_cap_w"] is None:
        missing.append("power_cap")
    if readings["pcie_current_width"] is None:
        missing.append("pcie_link")
    if readings["aer_correctable"] is None:
        missing.append("aer")

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
        power_cap_w=readings["power_cap_w"],
        energy_uj=readings["energy_uj"],
        sclk_mhz=readings["sclk_mhz"],
        sclk_max_mhz=readings["sclk_max_mhz"],
        mclk_mhz=readings["mclk_mhz"],
        vram_used_bytes=readings["vram_used_bytes"],
        vram_total_bytes=readings["vram_total_bytes"],
        gtt_used_bytes=readings["gtt_used_bytes"],
        gtt_total_bytes=readings["gtt_total_bytes"],
        gpu_busy_pct=readings["gpu_busy_pct"],
        mem_busy_pct=readings["mem_busy_pct"],
        hotspot_crit_c=readings["hotspot_crit_c"],
        pcie_current_speed_gts=readings["pcie_current_speed_gts"],
        pcie_max_speed_gts=readings["pcie_max_speed_gts"],
        pcie_current_width=readings["pcie_current_width"],
        pcie_max_width=readings["pcie_max_width"],
        aer_correctable=readings["aer_correctable"],
        aer_fatal=readings["aer_fatal"],
        aer_nonfatal=readings["aer_nonfatal"],
        throttle_flags=_derive_throttle_flags(readings),
        missing=missing,
    )
