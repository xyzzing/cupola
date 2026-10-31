"""Context size benchmarking and performance testing."""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cupola.discovery import find_llama_bench


@dataclass
class BenchResult:
    """Result of a benchmark run."""
    profile: str
    model: str
    gpu: Optional[str] = None
    context_size: int = 0
    tokens_per_sec: Optional[float] = None
    latency_first_token: Optional[float] = None
    vram_used_gb: Optional[float] = None
    vram_total_gb: Optional[float] = None
    vram_percent: Optional[float] = None
    success: bool = False
    error: Optional[str] = None
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "model": self.model,
            "gpu": self.gpu,
            "context_size": self.context_size,
            "tokens_per_sec": self.tokens_per_sec,
            "latency_first_token": self.latency_first_token,
            "vram_used_gb": self.vram_used_gb,
            "vram_total_gb": self.vram_total_gb,
            "vram_percent": self.vram_percent,
            "success": self.success,
            "error": self.error,
            "elapsed_seconds": self.elapsed_seconds,
        }


@dataclass
class ContextTestResult:
    """Result of context size testing."""
    max_working: int = 0
    failed_at: int = 0
    failed_reason: Optional[str] = None
    tests: list[BenchResult] = field(default_factory=list)


def get_llama_bench_path() -> Optional[Path]:
    """Find llama-bench binary using discovery."""
    return find_llama_bench()


def run_llama_bench(
    profile,
    context_size: Optional[int] = None,
    n_threads: int = 0,
    n_iterations: int = 2,
    n_prompts: int = 3,
) -> Optional[BenchResult]:
    """Run llama-bench with the given profile and context size.

    Args:
        profile: ProfileSpec to benchmark
        context_size: Override context size (use profile default if None)
        n_threads: Number of threads (0 = use profile default)
        n_iterations: Number of iterations for benchmarking
        n_prompts: Number of prompts to test

    Returns:
        BenchResult or None on failure
    """
    result = BenchResult(
        profile=profile.alias,
        model=str(profile.weights_path.name),
        context_size=context_size or profile.ctx_size,
    )

    bench_path = get_llama_bench_path()
    if not bench_path:
        result.error = "llama-bench binary not found"
        return result

    # Build command
    cmd = [
        str(bench_path),
        "-m", str(profile.weights_path),
        "-c", str(result.context_size),
        "-ngl", str(profile.n_gpu_layers),
        "-n", "128",  # Generate 128 tokens
        "-b", "512",  # Batch size
        "-ub", "128",  # Ubatch size
        "-t", str(n_threads or profile.threads),
        "-i", str(n_iterations),
        "-p", str(n_prompts),
    ]

    if profile.flash_attn:
        cmd.append("--flash-attn")

    # Run benchmark
    start_time = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600.0,  # 10 minute timeout
        )
        result.elapsed_seconds = time.time() - start_time

        if proc.returncode == 0:
            # Parse output
            result.success = True
            result.tokens_per_sec = _parse_tokens_per_sec(proc.stdout)
            result.latency_first_token = _parse_latency(proc.stdout)
            result.vram_used_gb, result.vram_total_gb = _parse_vram(proc.stdout)
            if result.vram_used_gb and result.vram_total_gb:
                result.vram_percent = (result.vram_used_gb / result.vram_total_gb) * 100
        else:
            result.error = f"llama-bench exited with code {proc.returncode}"
            # Check for OOM
            if "OOM" in proc.stderr or "out of memory" in proc.stderr.lower():
                result.error = "Out of memory (OOM)"
            elif "failed to load" in proc.stderr.lower():
                result.error = f"Failed to load model: {proc.stderr.strip()[-200:]}"
    except subprocess.TimeoutExpired:
        result.elapsed_seconds = time.time() - start_time
        result.error = "Benchmark timed out after 600s"
    except OSError as e:
        result.error = f"Failed to run llama-bench: {e}"

    return result


def _parse_tokens_per_sec(output: str) -> Optional[float]:
    """Parse tokens/sec from llama-bench output."""
    # Look for "tokens per second" or "t/s"
    match = re.search(r"([\d.]+)\s*tokens?\s*/\s*second", output)
    if match:
        return float(match.group(1))
    match = re.search(r"([\d.]+)\s*t/s", output)
    if match:
        return float(match.group(1))
    return None


def _parse_latency(output: str) -> Optional[float]:
    """Parse first token latency from llama-bench output."""
    # Look for "time to first token" or similar
    match = re.search(r"time to first token\s*[:=]\s*([\d.]+)\s*(ms|s)", output)
    if match:
        value = float(match.group(1))
        if match.group(2) == "ms":
            return value / 1000.0
        return value
    return None


def _parse_vram(output: str) -> tuple[Optional[float], Optional[float]]:
    """Parse VRAM usage from llama-bench output."""
    used = None
    total = None

    # Look for VRAM usage patterns
    match = re.search(r"VRAM used\s*[:=]\s*([\d.]+)\s*GB", output)
    if match:
        used = float(match.group(1))

    match = re.search(r"VRAM total\s*[:=]\s*([\d.]+)\s*GB", output)
    if match:
        total = float(match.group(1))

    return used, total


def test_context_sizes(
    profile,
    context_sizes: list[int],
) -> ContextTestResult:
    """Test multiple context sizes to find maximum working size.

    Args:
        profile: ProfileSpec to test
        context_sizes: List of context sizes to test (in increasing order)

    Returns:
        ContextTestResult with findings
    """
    result = ContextTestResult()

    for ctx_size in context_sizes:
        print(f"Testing context size: {ctx_size} tokens...")
        bench_result = run_llama_bench(profile, context_size=ctx_size)
        result.tests.append(bench_result)

        if bench_result.success:
            result.max_working = ctx_size
            print(f"  ✓ Success ({bench_result.elapsed_seconds:.1f}s)")
            if bench_result.tokens_per_sec:
                print(f"    Tokens/sec: {bench_result.tokens_per_sec:.1f}")
            if bench_result.vram_used_gb:
                print(f"    VRAM: {bench_result.vram_used_gb:.1f} GB")
        else:
            result.failed_at = ctx_size
            result.failed_reason = bench_result.error
            print(f"  ✗ Failed: {bench_result.error}")
            break

    return result


def bench_profile(
    profile,
    context_sizes: Optional[list[int]] = None,
) -> dict:
    """Run full benchmark for a profile.

    Args:
        profile: ProfileSpec to benchmark
        context_sizes: Optional list of context sizes to test

    Returns:
        Dict with benchmark results
    """
    # Run baseline benchmark with profile's default context size
    print(f"Running llama-bench with profile '{profile.alias}'...")
    print(f"Model: {profile.weights_path.name}")

    baseline = run_llama_bench(profile)

    # If context sizes specified, test them
    context_result = None
    if context_sizes:
        print("\nContext size test:")
        context_result = test_context_sizes(profile, context_sizes)

    return {
        "baseline": baseline,
        "context_test": context_result,
    }


def format_bench_output(results: dict) -> str:
    """Format benchmark results for display."""
    lines = []
    baseline = results["baseline"]

    lines.append(f"Benchmark results for profile '{baseline.profile}':")
    lines.append(f"  Model: {baseline.model}")
    lines.append(f"  Context size: {baseline.context_size}")

    if baseline.success:
        if baseline.tokens_per_sec:
            lines.append(f"  Tokens/sec: {baseline.tokens_per_sec:.1f}")
        if baseline.latency_first_token:
            lines.append(f"  Latency (first token): {baseline.latency_first_token:.1f}s")
        if baseline.vram_used_gb:
            if baseline.vram_total_gb:
                lines.append(
                    f"  VRAM used: {baseline.vram_used_gb:.1f} GB / "
                    f"{baseline.vram_total_gb:.1f} GB ({baseline.vram_percent:.0f}%)"
                )
            else:
                lines.append(f"  VRAM used: {baseline.vram_used_gb:.1f} GB")
    else:
        lines.append(f"  Error: {baseline.error}")

    context_test = results.get("context_test")
    if context_test:
        lines.append("\nContext size test:")
        if context_test.max_working:
            lines.append(f"  Max working: {context_test.max_working} tokens")
        if context_test.failed_at:
            lines.append(
                f"  Failed at: {context_test.failed_at} tokens "
                f"({context_test.failed_reason})"
            )

        # Recommendation
        if context_test.max_working:
            lines.append(
                f"\nRecommendation: Set ctx_size to {context_test.max_working} "
                f"for optimal performance"
            )

    return "\n".join(lines)
