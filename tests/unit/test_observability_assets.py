"""Guard tests for the shipped observability stack.

These files are read by Prometheus and Grafana, not by Python, so nothing else
in the suite would notice them breaking. The checks are deliberately textual:
the project has no YAML dependency, and a scrape that fails on a malformed
line is a runtime failure with no compile step behind it.
"""

import json
import re
from pathlib import Path

import pytest

from cupola.metrics import METRIC_META

REPO = Path(__file__).resolve().parents[2]
OBSERVABILITY = REPO / "observability"

COMPOSE = OBSERVABILITY / "docker-compose.yml"
PROMETHEUS_YML = OBSERVABILITY / "prometheus" / "prometheus.yml"
ALERTS_YML = OBSERVABILITY / "prometheus" / "alerts.yml"
DASHBOARD = OBSERVABILITY / "grafana" / "dashboards" / "cupola-throttling.json"
DATASOURCE = OBSERVABILITY / "grafana" / "provisioning" / "datasources" / "prometheus.yml"


def _read(path: Path) -> str:
    assert path.exists(), f"missing {path.relative_to(REPO)}"
    return path.read_text()


@pytest.fixture(scope="module")
def alerts_text() -> str:
    return _read(ALERTS_YML)


def _expr_of(block: str) -> str:
    """Read an ``expr:`` value, folded block scalar included.

    Multi-line expressions are written with ``>-`` because a throttling
    comparison that fits on one line is usually wrong. A single-line reader
    would silently see an empty expression.
    """
    lines = block.splitlines()
    index = next(i for i, line in enumerate(lines) if line.strip().startswith("expr:"))
    head = lines[index].split("expr:", 1)[1].strip()
    if head and not head.startswith((">", "|")):
        return head

    key_indent = len(lines[index]) - len(lines[index].lstrip())
    collected = []
    for line in lines[index + 1:]:
        if not line.strip():
            break
        if len(line) - len(line.lstrip()) <= key_indent:
            break
        collected.append(line.strip())
    return " ".join(collected)


@pytest.fixture(scope="module")
def alert_rules(alerts_text: str) -> list[dict]:
    """Pull each alert rule out as {name, expr, for} without a YAML parser."""
    rules = []
    for block in re.split(r"\n\s*- alert: ", alerts_text)[1:]:
        name = block.split("\n", 1)[0].strip()
        expr = _expr_of(block)
        window = re.search(r"^\s+for:\s+(\S+)", block, re.MULTILINE)
        assert expr, f"alert {name} has no expr"
        rules.append({"name": name, "expr": expr,
                      "for": window.group(1) if window else None})
    return rules


class TestStackWiring:

    def test_prometheus_scrapes_the_exporter_port(self):
        text = _read(PROMETHEUS_YML)
        assert "9817" in text, "the scrape target must match [metrics] port"
        assert "127.0.0.1:9817" in text, (
            "both services run with host networking, so the exporter is only "
            "reachable over loopback")

    def test_alert_rules_are_loaded(self):
        text = _read(PROMETHEUS_YML)
        assert "alerts.yml" in text
        assert ALERTS_YML.exists()

    def test_grafana_datasource_points_at_prometheus(self):
        text = _read(DATASOURCE)
        assert "127.0.0.1:9091" in text, (
            "Prometheus is published on loopback only; host networking means "
            "the datasource cannot use a container name")

    def test_grafana_password_is_not_committed(self):
        text = _read(COMPOSE)
        assert "GF_ADMIN_PASSWORD:?" in text, (
            "the password must come from the env file, with no default")
        assert (OBSERVABILITY / ".env.example").exists()
        assert "observability/.env" in _read(REPO / ".gitignore")

    def test_services_are_loopback_bound(self):
        """A metrics stack that listens on 0.0.0.0 exposes the whole box."""
        text = _read(COMPOSE)
        assert "127.0.0.1:9091" in text
        assert "GF_SERVER_HTTP_ADDR: 127.0.0.1" in text


class TestAlertRules:

    def test_every_alert_has_a_duration_or_is_instant(self, alert_rules):
        assert len(alert_rules) >= 10
        for rule in alert_rules:
            if rule["for"] is not None:
                assert re.fullmatch(r"\d+[msh]", rule["for"]), rule["name"]

    def test_rules_only_use_metrics_the_exporter_emits(self, alert_rules):
        """A typo in a metric name makes a rule permanently quiet, not loud."""
        known = set(METRIC_META)
        used = set()
        for rule in alert_rules:
            used.update(re.findall(r"\bsinter_[a-z0-9_]+", rule["expr"]))
        unknown = sorted(used - known)
        assert not unknown, f"rules reference metrics that are never emitted: {unknown}"

    def test_throttle_rules_watch_the_derived_flags(self, alert_rules):
        """No throttle-reason register exists on this ASIC, so the derived
        evidence is what has to carry the alert.

        Link degradation is the exception: there the two sides of the
        comparison are measurable, so the rule compares them directly and gets
        the real numbers into ``$value`` instead of a boolean.
        """
        exprs = " ".join(r["expr"] for r in alert_rules)
        assert "sinter_throttle_flag_info" in exprs
        for flag in ("clock_below_ceiling", "power_at_cap", "hotspot_at_crit"):
            assert flag in exprs, f"nothing alerts on {flag}"

    def test_hotspot_alerts_are_relative_to_the_hw_limit(self, alert_rules):
        """A fixed degree threshold is wrong hardware: the limit is read from
        the sensor, so the alert has to compare against it."""
        rule = next(r for r in alert_rules if r["name"] == "SinterHotspotHigh")
        assert "sinter_gpu_temperature_critical_celsius" in rule["expr"]

    def test_link_degradation_compares_negotiated_against_capable(self, alert_rules):
        rule = next(r for r in alert_rules if r["name"] == "SinterPcieLinkDegraded")
        assert "ignoring(limit)" in rule["expr"]
        assert "sinter_pcie_link_width" in rule["expr"]

    def test_exporter_down_is_its_own_alert(self, alert_rules):
        """Every other alert needs data, so blindness must be reported by a
        rule that does not depend on the exporter being up."""
        names = {r["name"] for r in alert_rules}
        assert "SinterExporterDown" in names
        assert "SinterSensorBlind" in names

    def test_blindness_rule_does_not_need_the_series_to_exist(self, alert_rules):
        rule = next(r for r in alert_rules if r["name"] == "SinterSensorBlind")
        assert "absent(" in rule["expr"]


class TestDashboard:

    @pytest.fixture(scope="class")
    def dashboard(self) -> dict:
        return json.loads(_read(DASHBOARD))

    def test_dashboard_is_provisionable(self, dashboard):
        assert dashboard["uid"] == "cupola-throttling"
        assert dashboard["title"]
        assert dashboard["panels"]

    def test_every_query_targets_the_provisioned_datasource(self, dashboard):
        """A panel wired to the default datasource silently queries nothing."""
        panels = list(dashboard["panels"])
        for panel in panels:
            panels.extend(panel.get("panels", []))
        queries = [t for p in panels for t in p.get("targets", [])]
        assert queries, "dashboard has no queries"
        assert all(q.get("datasource", {}).get("uid") == "prometheus" for q in queries)

    def test_dashboard_queries_only_emitted_metrics(self, dashboard):
        panels = list(dashboard["panels"])
        for panel in panels:
            panels.extend(panel.get("panels", []))
        used = set()
        for panel in panels:
            for target in panel.get("targets", []):
                used.update(re.findall(r"\bsinter_[a-z0-9_]+", target.get("expr", "")))
        unknown = sorted(used - set(METRIC_META))
        assert not unknown, f"dashboard queries metrics that are never emitted: {unknown}"

    def test_gaps_are_shown_instead_of_left_out(self, dashboard):
        """The dashboard must surface what is not measured, not quietly omit
        it: a missing SM-occupancy panel looks identical to a healthy GPU."""
        text = _read(DASHBOARD)
        assert "sinter_metric_unavailable_info" in text
        assert "Declared gaps" in text

    def test_refresh_is_faster_than_the_scrape_interval(self, dashboard):
        seconds = int(re.sub(r"\D", "", dashboard.get("refresh", "0")) or 0)
        assert 0 < seconds <= 10
