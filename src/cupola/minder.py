"""Minder cognitive policy engine.

Dynamically configures sampling flags and thinking token allocations
based on task consequence and real-time Cupola hardware telemetry.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from cupola.telemetry import TelemetryState, collect_telemetry


class ThinkingMode(Enum):
    """Cognitive thinking modes."""
    DIRECT = "direct"  # 0 thinking tokens
    LEAN = "lean"      # 512-1024 thinking tokens
    DEEP = "deep"      # 4096 thinking tokens


class TaskType(Enum):
    """Task consequence types."""
    INGESTION = "ingestion"       # Low/medium consequence
    PLANNING = "planning"         # High consequence
    EXECUTION = "execution"       # Medium consequence
    VERIFICATION = "verification" # Low consequence


@dataclass
class PolicyDecision:
    """Minder policy decision."""
    mode: ThinkingMode
    thinking_tokens: int
    temperature: float
    top_p: float
    pre_dispatch_delay: float
    reason: str


# Thermal thresholds
HOTSPOT_PACING_THRESHOLD = 88.0  # °C
VRAM_PACING_THRESHOLD = 1536     # MB (1.5GB)


def evaluate_policy(
    telemetry: Optional[TelemetryState] = None,
    task_type: TaskType = TaskType.INGESTION,
) -> PolicyDecision:
    """Evaluate Minder policy for a given task and telemetry state.

    Args:
        telemetry: Current telemetry state (collected if None).
        task_type: Type of task being performed.

    Returns:
        PolicyDecision with mode, tokens, and sampling parameters.
    """
    if telemetry is None:
        telemetry = collect_telemetry()

    # Thermal backpressure override
    if telemetry.pacing_active:
        # Clamp to LEAN or DIRECT based on severity
        if (
            telemetry.hotspot_celsius is not None
            and telemetry.hotspot_celsius > 92.0
        ):
            # Very hot: DIRECT only
            return PolicyDecision(
                mode=ThinkingMode.DIRECT,
                thinking_tokens=0,
                temperature=0.2,
                top_p=0.8,
                pre_dispatch_delay=2.0,
                reason=f"Thermal pacing active (hotspot {telemetry.hotspot_celsius}°C > 92°C)",
            )
        else:
            # Hot: LEAN only
            return PolicyDecision(
                mode=ThinkingMode.LEAN,
                thinking_tokens=1024,
                temperature=0.2,
                top_p=0.8,
                pre_dispatch_delay=1.0,
                reason=(
                    f"Thermal pacing active (hotspot "
                    f"{telemetry.hotspot_celsius}°C > "
                    f"{HOTSPOT_PACING_THRESHOLD}°C)"
                ),
            )

    # Nominal health state: evaluate task consequence
    if task_type == TaskType.PLANNING:
        # High consequence: DEEP mode for full DAG synthesis
        return PolicyDecision(
            mode=ThinkingMode.DEEP,
            thinking_tokens=4096,
            temperature=0.6,
            top_p=0.95,
            pre_dispatch_delay=0.0,
            reason="Plan DAG synthesis (high consequence)",
        )
    elif task_type == TaskType.INGESTION:
        # Low/medium consequence: LEAN mode for summarization
        return PolicyDecision(
            mode=ThinkingMode.LEAN,
            thinking_tokens=512,
            temperature=0.2,
            top_p=0.8,
            pre_dispatch_delay=0.0,
            reason="Document ingestion (low/medium consequence)",
        )
    elif task_type == TaskType.EXECUTION:
        # Medium consequence: LEAN mode
        return PolicyDecision(
            mode=ThinkingMode.LEAN,
            thinking_tokens=1024,
            temperature=0.2,
            top_p=0.8,
            pre_dispatch_delay=0.0,
            reason="Task execution (medium consequence)",
        )
    else:
        # Verification: DIRECT mode
        return PolicyDecision(
            mode=ThinkingMode.DIRECT,
            thinking_tokens=0,
            temperature=0.1,
            top_p=0.7,
            pre_dispatch_delay=0.0,
            reason="Verification (low consequence)",
        )


def apply_policy_delay(policy: PolicyDecision) -> None:
    """Apply pre-dispatch delay if required by policy."""
    if policy.pre_dispatch_delay > 0:
        time.sleep(policy.pre_dispatch_delay)


def get_sampling_params(policy: PolicyDecision) -> dict:
    """Get sampling parameters for LLM request."""
    return {
        "temperature": policy.temperature,
        "top_p": policy.top_p,
        "thinking_tokens": policy.thinking_tokens,
    }
