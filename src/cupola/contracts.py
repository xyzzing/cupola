"""Typed contracts at the cupola ⇄ minder boundary.

Contract-only in this PR (minder-memory plan PR 8): nothing in cupola calls
these yet, no LoRA is loaded anywhere, and no llama-server flag changes.
minder (the watchdog) proposes an :class:`InferenceIntent` derived from a
:class:`RuntimeSnapshot`; cupola (the substrate) answers with an
:class:`InferenceGrant`. ``adapter_preference`` is RECORDED on the grant and
deliberately NOT acted on — multi-LoRA on the primary GPU is out of scope
until the verified-episode loop produces real adapter candidates.

All objects use safe defaults: absent sensors become ``None``/``False``, so
constructing a snapshot from a machine with no telemetry never raises.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

# Thermal thresholds — imported values mirror cupola.minder so the clamp
# logic cannot drift from the policy engine's own backpressure rules.
from cupola.minder import HOTSPOT_PACING_THRESHOLD  # noqa: F401  (re-export)

VERY_HOT_CELSIUS = 92.0  # cupola.minder's direct-only threshold
LEAN_THINKING_TOKENS = 1024


@dataclass
class RuntimeSnapshot:
    """Point-in-time substrate state, tolerant of missing sensors."""

    ts: float = field(default_factory=time.time)
    gpu_name: Optional[str] = None
    vram_available_mb: Optional[int] = None
    vram_headroom_mb: Optional[int] = None
    hotspot_celsius: Optional[float] = None
    pacing_active: bool = False
    backend: Optional[str] = None          # e.g. "llama-server"
    model_id: Optional[str] = None
    adapters_loaded: tuple[str, ...] = ()  # names only; cupola loads none
    notes: dict = field(default_factory=dict)

    @classmethod
    def from_telemetry(cls, telemetry=None) -> "RuntimeSnapshot":
        """Best-effort snapshot from a TelemetryState (collected if None).

        Any failure — no sensors, collector crash, wrong shape — degrades to
        an all-unknown snapshot; contract objects must be constructible on
        any machine (plan PR 8 test 1).
        """
        if telemetry is None:
            try:
                from cupola.telemetry import collect_telemetry

                telemetry = collect_telemetry()
            except Exception:
                telemetry = None
        if telemetry is None:
            return cls()

        def get(name, default=None):
            return getattr(telemetry, name, default)

        return cls(
            ts=float(get("timestamp", time.time())),
            vram_available_mb=get("vram_available_mb"),
            vram_headroom_mb=get("vram_headroom_mb"),
            hotspot_celsius=get("hotspot_celsius"),
            pacing_active=bool(get("pacing_active", False)),
            notes={
                "quality": get("quality", "unavailable"),
                "gtt_spill_detected": bool(
                    get("gtt_spill_detected", False)),
                "missing": list(get("missing", []) or []),
            },
        )


@dataclass
class InferenceIntent:
    """What the agent wants to run, with its self-declared envelope."""

    task_type: str = "execution"  # ingestion|planning|execution|verification
    thinking_mode: str = "lean"   # direct|lean|deep
    thinking_tokens: int = 0
    max_tokens: int = 2048
    adapter_preference: Optional[str] = None  # recorded, never acted on
    allow_budget_override: bool = True


@dataclass
class InferenceGrant:
    """The substrate's answer: what may actually run."""

    granted: bool = True
    reason: str = ""
    thinking_tokens: int = 0
    max_tokens: int = 2048
    clamp_applied: bool = False
    adapter_requested: Optional[str] = None  # echoed from the intent
    adapter_loaded: Optional[str] = None     # always None in v1 (no LoRA)
    snapshot_ts: Optional[float] = None


def evaluate_intent(snapshot: RuntimeSnapshot, intent: InferenceIntent) -> InferenceGrant:
    """Pure contract-level clamp: thermal backpressure only.

    Mirrors cupola.minder.evaluate_policy's thermal override (pacing → LEAN,
    very hot → DIRECT) without any I/O. Nothing calls this in PR 8; it
    exists so the supervisor can adopt the typed seam without behaviour
    questions later.
    """
    grant = InferenceGrant(
        thinking_tokens=intent.thinking_tokens,
        max_tokens=intent.max_tokens,
        adapter_requested=intent.adapter_preference,
        adapter_loaded=None,  # v1: never load adapters
        snapshot_ts=snapshot.ts,
    )
    if snapshot.pacing_active:
        very_hot = (snapshot.hotspot_celsius is not None
                    and snapshot.hotspot_celsius > VERY_HOT_CELSIUS)
        cap = 0 if very_hot else LEAN_THINKING_TOKENS
        if grant.thinking_tokens > cap:
            grant.clamp_applied = True
            grant.thinking_tokens = cap
        grant.reason = (
            f"thermal pacing (hotspot "
            f"{snapshot.hotspot_celsius}°C)"
        )
    else:
        grant.reason = "nominal"
    return grant
