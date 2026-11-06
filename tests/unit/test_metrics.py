"""Tests for the Prometheus exposition: rendering, gaps, and the endpoint.

The contract these tests hold is narrow and deliberate: a value that could not
be measured must not appear as a number, and the scrape that could not measure
it must still be a 200 with the gap named.
"""

from __future__ import annotations

import http.client
import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from cupola.config import CupolaConfig, MetricsConfig
from cupola.metrics import (
    ASIC_GAPS,
    EventLedger,
    MetricsHandler,
    Sample,
    collect,
    render_samples,
    serve,
)
from cupola.sensors import SensorReading


def _reading(**overrides) -> SensorReading:
    base = dict(ts="2024-01-01T00:00:00+00:00", source="sysfs:card0", quality="ok",
                gpu_edge_c=65.0, gpu_hotspot_c=88.0, power_w=98.0, power_cap_w=339.0,
                gpu_busy_pct=84, sclk_mhz=2400, sclk_max_mhz=2526)
    base.update(overrides)
    return SensorReading(**base)


def _config(tmp_path: Path) -> CupolaConfig:
    config = CupolaConfig()
    config.state_dir = tmp_path
    return config


class TestRendering:
    def test_help_and_type_headers(self):
        body = render_samples([Sample("sinter_power_watts", 98.0)])
        assert "# HELP sinter_power_watts" in body
        assert "# TYPE sinter_power_watts gauge" in body
        assert "sinter_power_watts 98" in body

    def test_no_float_noise(self):
        body = render_samples([
            Sample("sinter_power_watts", 98.0),
            Sample("sinter_gpu_temperature_celsius", 65.5, {"sensor": "edge"}),
        ])
        assert "sinter_power_watts 98" in body
        assert 'sinter_gpu_temperature_celsius{sensor="edge"} 65.5' in body

    def test_labels_are_escaped(self):
        body = render_samples([
            Sample("sinter_metric_unavailable_info", 1,
                   {"name": "x", "reason": 'say "hi"\nthen \\ go'}),
        ])
        assert 'reason="say \\"hi\\"\\nthen \\\\ go"' in body

    def test_empty_render_is_empty(self):
        assert render_samples([]) == ""

    def test_unregistered_metric_is_still_emitted(self):
        """A name missing from the registry is visible, not hidden."""
        body = render_samples([Sample("sinter_forgotten_metric", 1)])
        assert "sinter_forgotten_metric 1" in body
        assert "# TYPE" not in body


class TestCollectGaps:
    def test_measured_values_are_emitted(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cupola.metrics.probe_sensors", lambda: _reading())
        names = {s.name for s in collect(_config(tmp_path))}
        assert {"sinter_gpu_temperature_celsius", "sinter_power_watts",
                "sinter_gpu_busy_percent", "sinter_power_cap_watts"} <= names

    def test_missing_sensor_is_declared_not_zeroed(self, tmp_path, monkeypatch):
        reading = _reading(gpu_busy_pct=None, power_w=None,
                           quality="degraded", missing=["busy", "power"])
        monkeypatch.setattr("cupola.metrics.probe_sensors", lambda: reading)

        samples = collect(_config(tmp_path))
        emitted = {s.name for s in samples}
        assert "sinter_gpu_busy_percent" not in emitted
        assert "sinter_power_watts" not in emitted

        gaps = {s.labels["name"] for s in samples
                if s.name == "sinter_metric_unavailable_info"}
        assert "sinter_sensor_busy" in gaps
        assert "sinter_sensor_power" in gaps

    def test_asic_gaps_are_always_declared(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cupola.metrics.probe_sensors", lambda: _reading())
        samples = collect(_config(tmp_path))
        gaps = {s.labels["name"]: s.labels["reason"] for s in samples
                if s.name == "sinter_metric_unavailable_info"}
        for name in ASIC_GAPS:
            assert gaps[name], f"{name} declared without a reason"

    def test_throttle_flags_are_published(self, tmp_path, monkeypatch):
        reading = _reading(throttle_flags=["power_at_cap", "clock_below_ceiling"])
        monkeypatch.setattr("cupola.metrics.probe_sensors", lambda: reading)

        flags = {s.labels["flag"] for s in collect(_config(tmp_path))
                 if s.name == "sinter_throttle_flag_info"}
        assert flags == {"power_at_cap", "clock_below_ceiling"}

    def test_backend_down_is_reported_as_zero(self, tmp_path, monkeypatch):
        """No supervised process is an observation, not a missing measurement."""
        monkeypatch.setattr("cupola.metrics.probe_sensors", lambda: _reading())
        samples = {s.name: s.value for s in collect(_config(tmp_path))
                   if not s.labels}
        assert samples["sinter_backend_up"] == 0
        assert samples["sinter_backend_launches_total"] == 0
        assert samples["sinter_backend_unexpected_exits_total"] == 0

    def test_telemetry_view_is_merged_in(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cupola.metrics.probe_sensors", lambda: _reading())
        current = tmp_path / "telemetry" / "current.json"
        current.parent.mkdir(parents=True)
        current.write_text(json.dumps({
            "schema_version": 2,
            "vram_available_mb": 996,
            "vram_headroom_mb": 0,
            "gtt_spill_detected": True,
            "pacing_active": False,
        }))

        values = {s.name: s.value for s in collect(_config(tmp_path)) if not s.labels}
        assert values["sinter_vram_available_bytes"] == 996 * 1024 * 1024
        assert values["sinter_vram_headroom_bytes"] == 0
        assert values["sinter_gtt_spill_detected"] == 1
        assert values["sinter_pacing_active"] == 0

    def test_absent_telemetry_emits_no_vram_gauges(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cupola.metrics.probe_sensors", lambda: _reading())
        names = {s.name for s in collect(_config(tmp_path))}
        assert "sinter_vram_available_bytes" not in names
        assert "sinter_gtt_spill_detected" not in names

    def test_unwritable_state_dir_degrades_instead_of_raising(self, tmp_path, monkeypatch):
        """Losing durability is reported; it does not take the scrape with it.

        The totals stay right inside the process, but ``increase()`` across an
        exporter restart stops working, so the loss has to be visible.
        """
        monkeypatch.setattr("cupola.metrics.probe_sensors", lambda: _reading())
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory")
        config = _config(tmp_path)
        config.state_dir = blocked

        values = {s.name: s.value for s in collect(config) if not s.labels}
        assert values["sinter_ledger_persist_error"] == 1
        assert values["sinter_backend_up"] == 0


class TestEndpoint:
    """The HTTP surface, on an ephemeral loopback port."""

    def _start(self, tmp_path, config=None):
        config = config or _config(tmp_path)
        ledger = EventLedger(config.state_dir, MetricsConfig())
        httpd = ThreadingHTTPServer((config.metrics.bind, 0),
                            partial(MetricsHandler, config=config, ledger=ledger))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, thread

    def _get(self, httpd, path):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        return resp, body

    def test_metrics_endpoint_shape(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cupola.metrics.probe_sensors", lambda: _reading())
        httpd, thread = self._start(tmp_path)
        try:
            resp, body = self._get(httpd, "/metrics")
            assert resp.status == 200
            assert resp.getheader("Content-Type") == "text/plain; version=0.0.4; charset=utf-8"
            assert "sinter_power_watts 98" in body
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()

    def test_landing_and_unknown_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cupola.metrics.probe_sensors", lambda: _reading())
        httpd, thread = self._start(tmp_path)
        try:
            resp, body = self._get(httpd, "/")
            assert resp.status == 200
            assert "/metrics" in body
            resp, _ = self._get(httpd, "/nope")
            assert resp.status == 404
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()

    def test_probe_failure_still_scrapes(self, tmp_path, monkeypatch):
        """A blind GPU must not turn into a missing target.

        A 500 here would make Prometheus show the whole host as down, hiding
        which part went blind and taking the backend counters with it.
        """
        def _boom():
            raise RuntimeError("sysfs unreadable")

        monkeypatch.setattr("cupola.metrics.probe_sensors", _boom)
        httpd, thread = self._start(tmp_path)
        try:
            resp, body = self._get(httpd, "/metrics")
            assert resp.status == 200
            assert 'sinter_sensor_quality_info{quality="unavailable"} 1' in body
            assert "sinter_backend_up 0" in body
            assert "sinter_scrape_error_total 1" in body
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()

    def test_serve_refuses_a_taken_port(self, tmp_path, monkeypatch):
        """A port clash is a startup failure, not a silent fallback."""
        httpd, thread = self._start(tmp_path)
        port = httpd.server_address[1]
        try:
            assert serve(_config(tmp_path), bind="127.0.0.1", port=port) == 1
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()
