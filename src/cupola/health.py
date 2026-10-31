"""GPU health monitoring and alarm system.

Tracks temperature, VRAM usage, and thermal throttling over time.
Raises alarms when thresholds are exceeded.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cupola.config import DEFAULT_STATE_DIR


@dataclass
class HealthThresholds:
    """Configurable health thresholds."""
    temp_warning_celsius: float = 80.0
    temp_critical_celsius: float = 90.0
    temp_emergency_celsius: float = 95.0
    vram_warning_percent: float = 85.0
    vram_critical_percent: float = 95.0
    thermal_throttle_duration_seconds: float = 300.0  # 5 min


@dataclass
class HealthAlert:
    """A health alert."""
    timestamp: float
    severity: str  # warning, critical, emergency
    metric: str
    value: float
    threshold: float
    message: str


@dataclass
class HealthHistory:
    """Historical health data."""
    temperatures: list[tuple[float, float]] = field(default_factory=list)  # (timestamp, temp)
    vram_usage: list[tuple[float, float]] = field(default_factory=list)  # (timestamp, percent)
    alerts: list[HealthAlert] = field(default_factory=list)
    thermal_throttle_start: Optional[float] = None


class HealthMonitor:
    """Monitors GPU health and raises alarms."""

    def __init__(self, thresholds: Optional[HealthThresholds] = None):
        self.thresholds = thresholds or HealthThresholds()
        self.history = HealthHistory()
        self.state_dir = DEFAULT_STATE_DIR
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def record_temperature(self, temp_celsius: float) -> list[HealthAlert]:
        """Record temperature and check for alerts."""
        now = time.time()
        self.history.temperatures.append((now, temp_celsius))

        # Keep last hour of data
        cutoff = now - 3600
        self.history.temperatures = [
            (t, v) for t, v in self.history.temperatures if t > cutoff
        ]

        alerts = []

        # Check thermal throttle duration
        if temp_celsius > self.thresholds.temp_warning_celsius:
            if self.history.thermal_throttle_start is None:
                self.history.thermal_throttle_start = now
            elif now - self.history.thermal_throttle_start > self.thresholds.thermal_throttle_duration_seconds:  # noqa: E501
                alert = HealthAlert(
                    timestamp=now,
                    severity="critical",
                    metric="thermal_throttle_duration",
                    value=now - self.history.thermal_throttle_start,
                    threshold=self.thresholds.thermal_throttle_duration_seconds,
                    message="GPU has been thermally throttled for over 5 minutes",
                )
                alerts.append(alert)
                self.history.alerts.append(alert)
        else:
            self.history.thermal_throttle_start = None

        # Temperature alerts
        if temp_celsius > self.thresholds.temp_emergency_celsius:
            alert = HealthAlert(
                timestamp=now,
                severity="emergency",
                metric="temperature",
                value=temp_celsius,
                threshold=self.thresholds.temp_emergency_celsius,
                message=f"GPU temperature critical: {temp_celsius:.1f}°C",
            )
            alerts.append(alert)
            self.history.alerts.append(alert)
        elif temp_celsius > self.thresholds.temp_critical_celsius:
            alert = HealthAlert(
                timestamp=now,
                severity="critical",
                metric="temperature",
                value=temp_celsius,
                threshold=self.thresholds.temp_critical_celsius,
                message=f"GPU temperature high: {temp_celsius:.1f}°C",
            )
            alerts.append(alert)
            self.history.alerts.append(alert)
        elif temp_celsius > self.thresholds.temp_warning_celsius:
            alert = HealthAlert(
                timestamp=now,
                severity="warning",
                metric="temperature",
                value=temp_celsius,
                threshold=self.thresholds.temp_warning_celsius,
                message=f"GPU temperature elevated: {temp_celsius:.1f}°C",
            )
            alerts.append(alert)
            self.history.alerts.append(alert)

        return alerts

    def record_vram_usage(self, used_gb: float, total_gb: float) -> list[HealthAlert]:
        """Record VRAM usage and check for alerts."""
        now = time.time()
        percent = (used_gb / total_gb) * 100.0
        self.history.vram_usage.append((now, percent))

        # Keep last hour
        cutoff = now - 3600
        self.history.vram_usage = [
            (t, v) for t, v in self.history.vram_usage if t > cutoff
        ]

        alerts = []

        if percent > self.thresholds.vram_critical_percent:
            alert = HealthAlert(
                timestamp=now,
                severity="critical",
                metric="vram_usage",
                value=percent,
                threshold=self.thresholds.vram_critical_percent,
                message=f"VRAM usage critical: {percent:.1f}%",
            )
            alerts.append(alert)
            self.history.alerts.append(alert)
        elif percent > self.thresholds.vram_warning_percent:
            alert = HealthAlert(
                timestamp=now,
                severity="warning",
                metric="vram_usage",
                value=percent,
                threshold=self.thresholds.vram_warning_percent,
                message=f"VRAM usage high: {percent:.1f}%",
            )
            alerts.append(alert)
            self.history.alerts.append(alert)

        return alerts

    def get_health_summary(self) -> dict:
        """Get current health summary."""
        now = time.time()

        # Recent temperatures (last 5 min)
        recent_temps = [
            v for t, v in self.history.temperatures if t > now - 300
        ]
        avg_temp = sum(recent_temps) / len(recent_temps) if recent_temps else None
        max_temp = max(recent_temps) if recent_temps else None

        # Recent VRAM usage
        recent_vram = [
            v for t, v in self.history.vram_usage if t > now - 300
        ]
        avg_vram = sum(recent_vram) / len(recent_vram) if recent_vram else None
        max_vram = max(recent_vram) if recent_vram else None

        # Recent alerts
        recent_alerts = [
            a for a in self.history.alerts if a.timestamp > now - 300
        ]

        return {
            "average_temperature_celsius": avg_temp,
            "max_temperature_celsius": max_temp,
            "average_vram_percent": avg_vram,
            "max_vram_percent": max_vram,
            "thermal_throttle_active": self.history.thermal_throttle_start is not None,
            "recent_alerts": len(recent_alerts),
        }

    def save_health_report(self, path: Optional[Path] = None) -> Path:
        """Save health report to file."""
        if path is None:
            path = self.state_dir / f"health_report_{int(time.time())}.json"

        report = {
            "timestamp": time.time(),
            "summary": self.get_health_summary(),
            "recent_temperatures": self.history.temperatures[-100:],
            "recent_vram_usage": self.history.vram_usage[-100:],
            "recent_alerts": [
                {
                    "timestamp": a.timestamp,
                    "severity": a.severity,
                    "metric": a.metric,
                    "value": a.value,
                    "message": a.message,
                }
                for a in self.history.alerts[-50:]
            ],
        }

        with open(path, "w") as f:
            json.dump(report, f, indent=2)

        return path