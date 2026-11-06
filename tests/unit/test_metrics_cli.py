"""CLI surface for ``cupola metrics``.

The command exists so the exact bytes a Prometheus scrape would see can be
inspected without a server, and so the exporter can be run in the foreground.
Both paths must survive a machine where some sensors are missing: the command
reports gaps, it never fails the way a missing metric would.
"""

import argparse
import json

import pytest

from cupola import cli
from cupola.config import CupolaConfig


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Replace config loading so no test touches the real state directory."""
    config = CupolaConfig()
    config.state_dir = tmp_path / "state"
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)
    return config


def _args(**overrides):
    base = {"bind": None, "port": None, "serve": False, "json": False}
    base.update(overrides)
    return argparse.Namespace(**base)


class TestOneShotOutput:

    def test_default_output_is_prometheus_text(self, isolated_config, capsys):
        assert cli.cmd_metrics(_args()) == 0
        out = capsys.readouterr().out

        assert out.startswith("# HELP ")
        assert "# TYPE sinter_backend_up gauge" in out
        assert "sinter_backend_up " in out

    def test_declared_gaps_are_printed_as_gaps(self, isolated_config, capsys, monkeypatch):
        """A metric this ASIC cannot produce must arrive as a gap, not a zero."""
        import cupola.metrics as metrics

        def blind(config, ledger=None):
            return [metrics.unavailable("sinter_pcie_tx_bytes_total",
                                        "no per-device byte counters on this ASIC")]

        monkeypatch.setattr(metrics, "collect", blind)
        assert cli.cmd_metrics(_args()) == 0
        out = capsys.readouterr().out

        assert 'sinter_metric_unavailable_info{name="sinter_pcie_tx_bytes_total"' in out
        assert "sinter_pcie_tx_bytes_total 0" not in out

    def test_json_output_is_machine_readable(self, isolated_config, capsys):
        assert cli.cmd_metrics(_args(json=True)) == 0
        payload = json.loads(capsys.readouterr().out)

        assert isinstance(payload, list)
        assert {"name", "value", "labels"} <= set(payload[0])
        assert any(s["labels"] for s in payload), "expected at least one labelled sample"

    def test_counters_are_durable_across_invocations(self, isolated_config, capsys):
        """Each run is a new process, so the ledger on disk is the only memory.

        Without it an OOM seen yesterday would be recounted on every scrape
        after a restart, and ``increase()`` over launches would reset.
        """
        logs = isolated_config.state_dir / "logs"
        logs.mkdir(parents=True)
        (logs / "backend-test.log").write_text(
            "ggml-alloc: failed to allocate 8192 MB\n")
        ledger_file = isolated_config.state_dir / "metrics" / "events.json"

        cli.cmd_metrics(_args())
        capsys.readouterr()
        assert json.loads(ledger_file.read_text())["counters"]["vram_oom_backend_log"] == 1

        cli.cmd_metrics(_args())
        out = capsys.readouterr().out
        assert json.loads(ledger_file.read_text())["counters"]["vram_oom_backend_log"] == 1
        assert 'sinter_vram_oom_events_total{source="backend_log"} 1' in out


class TestServe:

    def test_serve_hands_the_config_to_the_server(self, isolated_config, monkeypatch):
        seen = {}

        def fake_serve(config):
            seen["config"] = config
            return 0

        import cupola.metrics as metrics
        monkeypatch.setattr(metrics, "serve", fake_serve)

        assert cli.cmd_metrics(_args(serve=True, port=9911, bind="0.0.0.0")) == 0
        assert seen["config"].metrics.port == 9911
        assert seen["config"].metrics.bind == "0.0.0.0"

    def test_disabled_exporter_is_announced_not_silently_served(self, isolated_config,
                                                                monkeypatch, capsys):
        isolated_config.metrics.enabled = False
        monkeypatch.setattr("cupola.metrics.serve", lambda config: 0)

        assert cli.cmd_metrics(_args(serve=True)) == 0
        assert "enabled=false" in capsys.readouterr().out


class TestParser:

    def test_serve_and_json_are_mutually_exclusive(self):
        """One flag streams forever, the other prints once: not both."""
        with pytest.raises(SystemExit) as exc:
            cli.main(["metrics", "--serve", "--json"])
        assert exc.value.code == 2

    def test_subcommand_dispatches_to_the_metrics_command(self, isolated_config, capsys):
        """Parsing is only decoration if the subcommand does not route here."""
        assert cli.main(["metrics", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert any(s["name"] == "sinter_backend_up" for s in payload)
