"""Prometheus exposition for GPU throttling and VRAM pressure.

A 200 response says the server answered. It does not say the GPU kept its
clocks, stayed off its power cap, or had anywhere left to allocate. This module
publishes the counters that make the difference visible, in the shape the
dashboard and alert rules need.

Naming follows DCGM field semantics — ``gpu_temperature``, ``gpu_util``,
``fb_memory_usage``, ``pcie_link_*`` — so a dashboard written against
``dcgm-exporter`` transfers here. DCGM itself cannot run on this ASIC, and the
counters it would have supplied that this platform genuinely does not have are
not approximated into existence: they are published as
``sinter_metric_unavailable_info`` with the reason they are missing. A panel
that reads zero is worse than a panel that says it cannot see.
"""

from __future__ import annotations

import functools
import json
import os
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from cupola import __version__
from cupola.config import CupolaConfig, MetricsConfig
from cupola.sensors import probe_sensors
from cupola.state import StateRecord, load_state

#: Counters that DCGM would expose and this ASIC does not. Declared on every
#: scrape so the dashboard states its own blind spots instead of implying that
#: a flat line means a healthy device.
ASIC_GAPS: dict[str, str] = {
    "sinter_sm_occupancy_percent":
        "no CU occupancy counter on this ASIC; sinter_gpu_busy_percent is the proxy",
    "sinter_pcie_tx_bytes_total":
        "amdgpu exposes no PCIe TX/RX byte counters (amd-smi current_bandwidth_sent is N/A)",
    "sinter_pcie_rx_bytes_total":
        "amdgpu exposes no PCIe TX/RX byte counters (amd-smi current_bandwidth_received is N/A)",
    "sinter_gpu_throttle_reason_bits":
        "no throttle-reason accumulator on this ASIC; read sinter_throttle_flag_info instead",
}

#: Supervisor events that mean a human or cupola asked the backend to stop.
#: Anything else that ends a live backend is an unexpected exit.
DELIBERATE_STOP_EVENTS = frozenset({
    "stop_starting", "stop_sigterm", "stop_sigkill", "stop_completed",
    "stop_degraded", "sentinel_critical_stop",
})

#: States in which the supervised process is expected to be running.
RUNNING_STATES = frozenset({"VALIDATING", "STARTING", "READY", "STOPPING", "DEGRADED"})

#: Metric name -> (type, help). Rendered as ``# TYPE`` / ``# HELP`` headers.
#:
#: The label-carrying ``*_info`` metrics are declared ``gauge``, not ``info``:
#: ``# TYPE x info`` is OpenMetrics, and Prometheus rejects it in the 0.0.4 text
#: format this endpoint serves. The pattern (a constant 1 whose labels carry the
#: meaning) is how node_exporter publishes identity too.
METRIC_META: dict[str, tuple[str, str]] = {
    "sinter_gpu_temperature_celsius": ("gauge", "GPU temperature by sensor role."),
    "sinter_gpu_temperature_critical_celsius": (
        "gauge", "Hardware critical limit for the same sensor role."),
    "sinter_gpu_busy_percent": ("gauge", "Engine busy percent; occupancy proxy on this ASIC."),
    "sinter_mem_busy_percent": ("gauge", "VRAM controller busy percent."),
    "sinter_clock_mhz": ("gauge", "Clock by role; sclk_max is the DPM ceiling."),
    "sinter_power_watts": ("gauge", "Board power draw."),
    "sinter_power_cap_watts": ("gauge", "Power limit the draw is held under."),
    "sinter_vram_used_bytes": ("gauge", "Frame buffer bytes in use."),
    "sinter_vram_total_bytes": ("gauge", "Frame buffer capacity."),
    "sinter_vram_available_bytes": ("gauge", "Frame buffer reported free by the backend."),
    "sinter_vram_headroom_bytes": (
        "gauge", "Free frame buffer minus the configured launch reserve."),
    "sinter_gtt_used_bytes": ("gauge", "GTT aperture in use; growth is spill."),
    "sinter_gtt_total_bytes": ("gauge", "GTT aperture capacity."),
    "sinter_gtt_spill_detected": ("gauge", "1 when the sentinel saw GTT grow."),
    "sinter_pacing_active": ("gauge", "1 while the sentinel is throttling intake."),
    "sinter_pcie_link_speed_gts": ("gauge", "Negotiated PCIe link speed; limit=current|max."),
    "sinter_pcie_link_width": ("gauge", "Negotiated PCIe lane count; limit=current|max."),
    "sinter_pcie_aer_errors_total": ("counter", "PCIe AER error totals since boot."),
    "sinter_backend_up": ("gauge", "1 while the supervised backend process is alive."),
    "sinter_backend_state_info": ("gauge", "Recorded supervisor state."),
    "sinter_backend_launches_total": ("counter", "Backends cupola spawned."),
    "sinter_backend_unexpected_exits_total": (
        "counter", "Backends that died without a stop request."),
    "sinter_vram_oom_events_total": ("counter", "Allocation failures observed."),
    "sinter_scrape_error_total": ("counter", "Scrapes that hit a probe failure."),
    "sinter_ledger_persist_error": ("gauge", "Event ledger could not be written to the state dir."),
    "sinter_sensor_quality_info": ("gauge", "Overall sensor quality from this scrape."),
    "sinter_throttle_flag_info": (
        "gauge", "Derived limit evidence; inferred, not driver-reported."),
    "sinter_metric_unavailable_info": ("gauge", "Declared gap: name and reason."),
    "sinter_instance_info": ("gauge", "Supervised instance identity."),
    "sinter_exporter_info": ("gauge", "Exporter version."),
}


@dataclass(frozen=True)
class Sample:
    """One metric line, with the metadata needed to render it."""
    name: str
    value: float
    labels: dict[str, str] = field(default_factory=dict)


def _escape_label(value: str) -> str:
    """Escape a label value per the Prometheus text format."""
    return (value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n"))


def _format_value(value: float) -> str:
    """Render a value without float noise: 98.0 becomes 98, not 98.000000."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.3f}"


def render_samples(samples: list[Sample]) -> str:
    """Render samples in the Prometheus text exposition format.

    HELP/TYPE come from :data:`METRIC_META`. A name that is not registered is
    still emitted — an unregistered metric is a bug worth seeing in the output,
    not one worth hiding.
    """
    grouped: dict[str, list[Sample]] = {}
    for sample in samples:
        grouped.setdefault(sample.name, []).append(sample)

    lines: list[str] = []
    for name in sorted(grouped):
        meta = METRIC_META.get(name)
        if meta:
            lines.append(f"# HELP {name} {meta[1]}")
            lines.append(f"# TYPE {name} {meta[0]}")
        for sample in grouped[name]:
            if sample.labels:
                rendered = ",".join(
                    f'{key}="{_escape_label(value)}"'
                    for key, value in sorted(sample.labels.items())
                )
                lines.append(f"{name}{{{rendered}}} {_format_value(sample.value)}")
            else:
                lines.append(f"{name} {_format_value(sample.value)}")
    return "\n".join(lines) + "\n" if lines else ""


def unavailable(name: str, reason: str) -> Sample:
    """Declare that a metric cannot be produced, and say why."""
    return Sample("sinter_metric_unavailable_info", 1, {"name": name, "reason": reason})


def _process_alive(pid: Optional[int]) -> bool:
    """Signal 0 liveness check. PermissionError means the process exists."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class EventLedger:
    """Counters built by tailing cupola's own logs, persisted across restarts.

    The counters have to outlive the exporter. A Prometheus counter that
    restarts from zero on every exporter reload turns ``increase()`` into noise,
    and the events it counts — an OOM, a backend that died on its own — are
    exactly the rare ones a reload is likely to erase.

    Each log is read from a stored byte cursor. The cursor resets when the
    inode changes (rotation) or the file shrinks (truncation), and a trailing
    partial line is left unconsumed so a signature split across scrapes is not
    lost.
    """

    def __init__(self, state_dir: Path, config: MetricsConfig):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "metrics" / "events.json"
        self.tail_cap = max(0, int(config.log_tail_max_bytes))
        self.patterns = [re.compile(p, re.IGNORECASE) for p in config.oom_patterns]
        self._lock = threading.Lock()
        self.persist_error = False
        self._data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, "r") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "cursors": raw.get("cursors") if isinstance(raw.get("cursors"), dict) else {},
            "counters": raw.get("counters") if isinstance(raw.get("counters"), dict) else {},
            "last_seen": raw.get("last_seen") if isinstance(raw.get("last_seen"), dict) else {},
        }

    def _save(self) -> None:
        """Persist the ledger, or record that persistence is impossible.

        Counters are kept in memory either way, so an unwritable state dir
        costs durability across restarts but not the current totals. That loss
        is published as a metric rather than raising: a scrape must still
        return, and the reader must know ``increase()`` over a restart is now
        unreliable.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.tmp")
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError:
            self.persist_error = True

    def _count(self, key: str, amount: int = 1) -> None:
        self._data["counters"][key] = int(self._data["counters"].get(key, 0)) + amount

    def _tail(self, path: Path) -> list[str]:
        """Complete lines appended to ``path`` since the last scrape."""
        try:
            st = path.stat()
        except OSError:
            return []

        cursor = self._data["cursors"].get(str(path)) or {}
        offset = 0
        if cursor.get("inode") == st.st_ino and int(cursor.get("offset", 0)) <= st.st_size:
            offset = int(cursor["offset"])

        available = st.st_size - offset
        if available <= 0:
            self._data["cursors"][str(path)] = {"inode": st.st_ino, "offset": offset}
            return []

        budget = min(available, self.tail_cap)
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read(budget)
        except OSError:
            return []

        cut = chunk.rfind(b"\n")
        if cut == -1:
            # No complete line yet. Leave the cursor where it is.
            return []

        consumed = chunk[:cut + 1]
        self._data["cursors"][str(path)] = {"inode": st.st_ino, "offset": offset + len(consumed)}
        return [line.decode("utf-8", "replace") for line in consumed.splitlines()]

    def _scan_backend_logs(self, log_dir: Path) -> int:
        """Count OOM signatures in backend logs. Returns the number of hits."""
        hits = 0
        if not log_dir.is_dir():
            return hits
        for log in sorted(log_dir.glob("backend-*.log")):
            for line in self._tail(log):
                if any(p.search(line) for p in self.patterns):
                    hits += 1
        return hits

    def _scan_operational_log(self, log_path: Path) -> set:
        """Count launches; return the pids that a stop request named."""
        stopped: set = set()
        for line in self._tail(log_path):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            event = record.get("event")
            if event == "launch_spawned":
                self._count("launches")
            elif event in DELIBERATE_STOP_EVENTS:
                pid = record.get("pid")
                if isinstance(pid, int):
                    stopped.add(pid)
        return stopped

    def update(self, state) -> dict:
        """Advance every cursor and return the persisted counters."""
        with self._lock:
            log_dir = self.state_dir / "logs"
            oom_log_hits = self._scan_backend_logs(log_dir)
            stopped_pids = self._scan_operational_log(log_dir / "cupola.log")

            if oom_log_hits:
                self._count("vram_oom_backend_log", oom_log_hits)

            pid = getattr(state, "backend_pid", None)
            alive = _process_alive(pid)
            recorded = getattr(state, "state", "STOPPED")

            last = self._data["last_seen"]
            was_alive = bool(last.get("alive"))
            same_process = last.get("pid") == pid and pid is not None
            if was_alive and same_process and not alive:
                # The process we saw running is gone. A stop request explains
                # it; anything else is a death worth alerting on.
                deliberate = pid in stopped_pids or recorded == "STOPPED"
                if not deliberate:
                    self._count("unexpected_exits")
                    if oom_log_hits:
                        # The log said allocation failed and the process then
                        # went away. That is an OOM death, not just a death.
                        self._count("vram_oom_process_exit")
            self._data["last_seen"] = {"pid": pid, "alive": alive}

            self._save()
            return dict(self._data["counters"])

    def counters(self) -> dict:
        """The persisted totals, without advancing any cursor."""
        return dict(self._data["counters"])

    def note_scrape_error(self) -> None:
        """Record that a probe raised during this scrape."""
        with self._lock:
            self._count("scrape_errors")
            self._save()


def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sensor_samples(reading) -> list[Sample]:
    """Translate a :class:`~cupola.sensors.SensorReading` into metrics.

    A value that is ``None`` is not emitted. Emitting zero for a sensor that
    could not be read is how a dashboard ends up showing a cool, idle, healthy
    GPU that is in fact not being measured at all.
    """
    samples: list[Sample] = []

    temps = (
        ("edge", reading.gpu_edge_c),
        ("junction", reading.gpu_hotspot_c),
        ("mem", reading.gpu_mem_c),
    )
    for role, value in temps:
        if value is not None:
            samples.append(Sample("sinter_gpu_temperature_celsius", value, {"sensor": role}))
    if reading.hotspot_crit_c is not None:
        samples.append(Sample("sinter_gpu_temperature_critical_celsius",
                              reading.hotspot_crit_c, {"sensor": "junction"}))

    if reading.gpu_busy_pct is not None:
        samples.append(Sample("sinter_gpu_busy_percent", reading.gpu_busy_pct))
    if reading.mem_busy_pct is not None:
        samples.append(Sample("sinter_mem_busy_percent", reading.mem_busy_pct))

    clocks = (("sclk", reading.sclk_mhz), ("mclk", reading.mclk_mhz),
              ("sclk_max", reading.sclk_max_mhz))
    for role, value in clocks:
        if value is not None:
            samples.append(Sample("sinter_clock_mhz", value, {"clock": role}))

    if reading.power_w is not None:
        samples.append(Sample("sinter_power_watts", reading.power_w))
    if reading.power_cap_w is not None:
        samples.append(Sample("sinter_power_cap_watts", reading.power_cap_w))

    for name, value in (("sinter_vram_used_bytes", reading.vram_used_bytes),
                        ("sinter_vram_total_bytes", reading.vram_total_bytes),
                        ("sinter_gtt_used_bytes", reading.gtt_used_bytes),
                        ("sinter_gtt_total_bytes", reading.gtt_total_bytes)):
        if value is not None:
            samples.append(Sample(name, value))

    aer = (("correctable", reading.aer_correctable), ("fatal", reading.aer_fatal),
           ("nonfatal", reading.aer_nonfatal))
    for kind, value in aer:
        if value is not None:
            samples.append(Sample("sinter_pcie_aer_errors_total", value, {"type": kind}))

    for limit, value in (("current", reading.pcie_current_speed_gts),
                         ("max", reading.pcie_max_speed_gts)):
        if value is not None:
            samples.append(Sample("sinter_pcie_link_speed_gts", value, {"limit": limit}))
    for limit, value in (("current", reading.pcie_current_width),
                         ("max", reading.pcie_max_width)):
        if value is not None:
            samples.append(Sample("sinter_pcie_link_width", value, {"limit": limit}))

    for flag in reading.throttle_flags:
        samples.append(Sample("sinter_throttle_flag_info", 1, {"flag": flag}))

    samples.append(Sample("sinter_sensor_quality_info", 1, {"quality": reading.quality}))

    # Name every gap this scrape produced, so absence is visible as itself.
    for name, reason in ASIC_GAPS.items():
        samples.append(unavailable(name, reason))
    for missing in reading.missing:
        samples.append(unavailable(f"sinter_sensor_{missing}",
                                   "sysfs node absent or unreadable on this host"))

    return samples


def telemetry_samples(state_dir: Path) -> list[Sample]:
    """Backend-side VRAM state written by the sentinel.

    This is the allocation view — what the serving process reports — as
    distinct from the sysfs view above, which is the device view. They disagree
    during spill, and the disagreement is the signal.
    """
    data = _read_json(Path(state_dir) / "telemetry" / "current.json")
    if data is None:
        return []

    samples: list[Sample] = []
    for key, name, scale in (("vram_available_mb", "sinter_vram_available_bytes", 1024 * 1024),
                             ("vram_headroom_mb", "sinter_vram_headroom_bytes", 1024 * 1024)):
        value = data.get(key)
        if isinstance(value, (int, float)):
            samples.append(Sample(name, value * scale))

    for key, name in (("gtt_spill_detected", "sinter_gtt_spill_detected"),
                      ("pacing_active", "sinter_pacing_active")):
        if key in data:
            samples.append(Sample(name, 1 if data[key] else 0))

    return samples


def backend_samples(state, counters: dict) -> list[Sample]:
    """Supervisor state plus the persisted event counters."""
    samples: list[Sample] = []

    alive = _process_alive(state.backend_pid)
    up = 1 if alive and state.state in RUNNING_STATES else 0
    samples.append(Sample("sinter_backend_up", up))
    samples.append(Sample("sinter_backend_state_info", 1, {"state": state.state or "STOPPED"}))

    if state.instance_id:
        samples.append(Sample("sinter_instance_info", 1, {
            "instance_id": state.instance_id,
            "profile": state.profile_alias or "unknown",
        }))

    # Event counters are always emitted. Zero events observed is a measurement,
    # not a gap, and an alert rule over a series that does not exist yet is
    # silently empty rather than plainly quiet.
    samples.append(Sample("sinter_backend_launches_total", int(counters.get("launches", 0))))
    samples.append(Sample("sinter_backend_unexpected_exits_total",
                          int(counters.get("unexpected_exits", 0))))
    samples.append(Sample("sinter_scrape_error_total", int(counters.get("scrape_errors", 0))))
    samples.append(Sample("sinter_vram_oom_events_total",
                          int(counters.get("vram_oom_backend_log", 0)),
                          {"source": "backend_log"}))
    samples.append(Sample("sinter_vram_oom_events_total",
                          int(counters.get("vram_oom_process_exit", 0)),
                          {"source": "process_exit"}))

    return samples


def collect(config: CupolaConfig, ledger: Optional[EventLedger] = None) -> list[Sample]:
    """Gather every sample, tolerating a failure in any one source.

    A scrape that cannot read the GPU still reports the backend, and still says
    the GPU is missing. Returning 500 would hide which part went blind.

    The ledger is advanced first and its counters read last, so a scrape that
    fails records its own failure rather than reporting it one scrape late.
    """
    samples: list[Sample] = []
    errors = 0

    active = ledger or EventLedger(config.state_dir, config.metrics)

    state = None
    counters: dict = {}
    try:
        state = load_state(config.state_dir)
        counters = active.update(state)
    except Exception:
        errors += 1

    try:
        samples.extend(sensor_samples(probe_sensors()))
    except Exception:
        errors += 1
        samples.append(Sample("sinter_sensor_quality_info", 1, {"quality": "unavailable"}))

    try:
        samples.extend(telemetry_samples(config.state_dir))
    except Exception:
        errors += 1

    if errors:
        active.note_scrape_error()
        counters = active.counters()

    samples.extend(backend_samples(state or StateRecord(), counters))
    if active.persist_error:
        samples.append(Sample("sinter_ledger_persist_error", 1))
    samples.append(Sample("sinter_exporter_info", 1, {"version": __version__}))
    return samples


LANDING_PAGE = """<!doctype html>
<html><head><title>cupola metrics</title></head>
<body>
<h1>cupola metrics</h1>
<p><a href="/metrics">/metrics</a></p>
</body></html>
"""


class MetricsHandler(BaseHTTPRequestHandler):
    """Serves ``/metrics``. Never 500s: a partial scrape beats a missing one."""

    def __init__(self, *args, config: CupolaConfig, ledger: EventLedger, **kwargs):
        self.config = config
        self.ledger = ledger
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            body = render_samples(collect(self.config, self.ledger)).encode("utf-8")
            self._respond(200, "text/plain; version=0.0.4; charset=utf-8", body)
        elif path == "/":
            self._respond(200, "text/html; charset=utf-8", LANDING_PAGE.encode("utf-8"))
        else:
            self._respond(404, "text/plain; charset=utf-8", b"not found\n")

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # noqa: A002 - stdlib signature
        """Silence per-request logging; a 10s scrape is 8640 lines a day."""


def serve(config: CupolaConfig, bind: Optional[str] = None,
          port: Optional[int] = None) -> int:
    """Run the exporter in the foreground. Returns a process exit code."""
    m = config.metrics
    host = bind or m.bind
    listen_port = int(port or m.port)

    ledger = EventLedger(config.state_dir, m)
    handler = functools.partial(MetricsHandler, config=config, ledger=ledger)

    try:
        httpd = ThreadingHTTPServer((host, listen_port), handler)
    except OSError as exc:
        print(f"metrics: cannot bind {host}:{listen_port}: {exc}")
        return 1

    print(f"cupola metrics on http://{host}:{listen_port}/metrics")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
