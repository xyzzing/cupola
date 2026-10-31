"""CUPOLA command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cupola import __version__
from cupola.backend import detect_features, format_backend_info
from cupola.bench import bench_profile, format_bench_output
from cupola.config import load_config, validate_profile
from cupola.gguf import read_gguf_header
from cupola.hardware import probe
from cupola.lock import acquire_lock
from cupola.memory import check_admission
from cupola.ramdisk import (
    format_ramdisk_info,
    list_models_on_ramdisk,
    probe_ramdisk,
    remove_from_ramdisk,
    transfer_to_ramdisk,
)
from cupola.sandbox import run_sandboxed
from cupola.sensors import probe_sensors
from cupola.setup import run_setup_wizard
from cupola.supervisor import Supervisor
from cupola.update import format_update_result, update_backend


def cmd_doctor(args: argparse.Namespace) -> int:
    """Read-only hardware/backend capability report."""
    info = probe()

    # Detect backend features
    config = load_config()
    backend_features = None
    if config.profiles:
        # Use the first profile's backend binary for feature detection
        first_profile = next(iter(config.profiles.values()))
        backend_features = detect_features(first_profile.backend_binary)

    # One-shot sensor probe for doctor
    try:
        sensor_reading = probe_sensors()
        sentinel_info = {
            "quality": sensor_reading.quality,
            "gpu_edge_c": sensor_reading.gpu_edge_c,
            "gpu_hotspot_c": sensor_reading.gpu_hotspot_c,
            "power_w": sensor_reading.power_w,
            "missing": sensor_reading.missing,
        }
    except Exception as e:
        sentinel_info = {
            "quality": "error",
            "error": str(e),
        }

    # Probe RAM disk
    ramdisk_info = None
    if config.ramdisk.enabled and config.ramdisk.path:
        try:
            from pathlib import Path as P
            ramdisk_info = probe_ramdisk(
                P(config.ramdisk.path),
                warn_pct=config.ramdisk.warn_used_pct,
                critical_pct=config.ramdisk.critical_used_pct,
            )
        except Exception:
            pass

    result = {
        "cupola_version": __version__,
        "os": info.os_release,
        "kernel": info.kernel,
        "cpu": {
            "model": info.cpu_model,
            "cores": info.cpu_cores,
            "threads": info.cpu_threads,
        },
        "ram": {
            "total_gb": round(info.ram_total_gb, 1),
            "available_gb": round(info.ram_available_gb, 1),
        },
        "gpu": {
            "name": info.gpu_name,
            "pci": info.gpu_pci,
            "vram_total_gb": round(info.gpu_vram_total_gb, 1) if info.gpu_vram_total_gb else None,
            "vram_used_gb": round(info.gpu_vram_used_gb, 1) if info.gpu_vram_used_gb else None,
        },
        "rocm": {
            "version": info.rocm_version,
        },
        "sentinel": sentinel_info,
        "llama_server": {
            "running": info.llama_server_running,
            "port": info.llama_server_port,
            "version": info.llama_server_version,
            "features": backend_features.to_dict() if backend_features else None,
        },
        "ramdisk": ramdisk_info.to_dict() if ramdisk_info else None,
        "errors": info.errors,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"CUPOLA doctor v{__version__}")
        print(f"  OS: {info.os_release}")
        print(f"  Kernel: {info.kernel}")
        print(f"  CPU: {info.cpu_model} ({info.cpu_cores} cores / {info.cpu_threads} threads)")
        print(f"  RAM: {info.ram_total_gb:.1f} GB total, {info.ram_available_gb:.1f} GB available")
        if info.gpu_name:
            print(f"  GPU: {info.gpu_name} ({info.gpu_pci})")
            if info.gpu_vram_total_gb:
                print(
                    f"  VRAM: {info.gpu_vram_used_gb:.1f} GB / {info.gpu_vram_total_gb:.1f} GB used"
                )
        else:
            print("  GPU: not detected")
        print(f"  ROCm: {info.rocm_version or 'not detected'}")

        # Sentinel info
        sentinel = result.get("sentinel", {})
        if sentinel.get("quality") == "error":
            print(f"  Sentinel: error ({sentinel.get('error', 'unknown')})")
        elif sentinel.get("quality") == "unavailable":
            print("  Sentinel: unavailable (no GPU detected)")
        else:
            print(f"  Sentinel: {sentinel.get('quality', 'unknown')}")
            if sentinel.get("gpu_hotspot_c") is not None:
                print(f"    Hotspot: {sentinel['gpu_hotspot_c']:.1f}°C")
            if sentinel.get("gpu_edge_c") is not None:
                print(f"    Edge: {sentinel['gpu_edge_c']:.1f}°C")
            if sentinel.get("power_w") is not None:
                print(f"    Power: {sentinel['power_w']:.1f} W")
            if sentinel.get("missing"):
                print(f"    Missing: {', '.join(sentinel['missing'])}")

        if info.llama_server_running:
            print(f"  llama-server: running on port {info.llama_server_port}")
        else:
            print("  llama-server: not running")

        if backend_features:
            print()
            print(format_backend_info(backend_features))
        elif info.llama_server_version:
            print(f"  llama-server version: {info.llama_server_version}")

        # RAM disk info
        if ramdisk_info:
            print()
            print(format_ramdisk_info(ramdisk_info))

        if info.errors:
            print("  Errors:")
            for err in info.errors:
                print(f"    - {err}")
        else:
            print("  Status: OK")

    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate a profile specification without loading weights."""
    config = load_config()
    if args.profile not in config.profiles:
        result = {
            "profile": args.profile,
            "valid": False,
            "errors": ["Profile not found in configuration"],
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Error: profile '{args.profile}' not found in configuration")
            print(f"Available profiles: {', '.join(config.profiles.keys())}")
        return 1

    profile = config.profiles[args.profile]
    errors = validate_profile(profile)

    # Check backend feature compatibility
    warnings = []
    features = detect_features(profile.backend_binary)

    if profile.flash_attn and features.flash_attn is False:
        warnings.append(
            "Profile uses flash_attn but binary doesn't support it. "
            "Run 'cupola update --backend' to improve performance."
        )

    if profile.cache_type_k not in features.cache_types and features.cache_types:
        warnings.append(
            f"Profile uses cache_type_k={profile.cache_type_k} but binary may not support it. "
            f"Supported: {', '.join(features.cache_types)}"
        )

    if profile.cache_type_v not in features.cache_types and features.cache_types:
        warnings.append(
            f"Profile uses cache_type_v={profile.cache_type_v} but binary may not support it. "
            f"Supported: {', '.join(features.cache_types)}"
        )

    if errors:
        result = {
            "profile": args.profile,
            "valid": False,
            "errors": errors,
            "warnings": warnings,
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Validation FAILED for profile '{args.profile}':")
            for err in errors:
                print(f"  - {err}")
            if warnings:
                print("  Warnings:")
                for warn in warnings:
                    print(f"  - {warn}")
        return 1
    else:
        result = {
            "profile": args.profile,
            "valid": True,
            "details": {
                "weights": str(profile.weights_path),
                "backend": str(profile.backend_binary),
                "device": profile.device,
                "context": profile.ctx_size,
                "gpu_layers": profile.n_gpu_layers,
                "port": profile.port,
            },
            "warnings": warnings,
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Validation PASSED for profile '{args.profile}'")
            print(f"  Weights: {profile.weights_path}")
            print(f"  Backend: {profile.backend_binary}")
            print(f"  Device: {profile.device}")
            print(f"  GPU layers: {profile.n_gpu_layers}")
            print(f"  Context: {profile.ctx_size}")
            if warnings:
                print("  Warnings:")
                for warn in warnings:
                    print(f"  - {warn}")
        return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Explain requested vs effective settings for a profile."""
    config = load_config()
    if args.profile not in config.profiles:
        print(f"Error: profile '{args.profile}' not found in configuration")
        print(f"Available profiles: {', '.join(config.profiles.keys())}")
        return 1

    profile = config.profiles[args.profile]
    errors = validate_profile(profile)

    # Try llama-server API first for model info
    gguf_info = None
    if profile.weights_path.exists():
        try:
            import urllib.error
            import urllib.request

            req = urllib.request.Request(f"http://{profile.host}:{profile.port}/v1/models")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                # Try data[].meta first (richer info), then models[].details
                for m in data.get("data", []):
                    meta = m.get("meta", {})
                    if meta:
                        gguf_info = type("GGUFInfo", (), {
                            "total_bytes": meta.get("size", 0),
                            "arch": "llama",
                            "params": meta.get("n_params", 0),
                            "n_layers": meta.get("n_layers", 0),
                            "n_heads": meta.get("n_heads", 0),
                            "n_kv_heads": meta.get("n_kv_heads", 0),
                            "embedding_dim": meta.get("n_embd", 0),
                            "context_length": meta.get("n_ctx", 0),
                            "errors": [],
                        })()
                        break
                if gguf_info is None:
                    for m in data.get("models", []):
                        details = m.get("details", {})
                        if details.get("format") == "gguf":
                            gguf_info = type("GGUFInfo", (), {
                                "total_bytes": details.get("size", 0),
                                "arch": details.get("family", ""),
                                "params": details.get("n_params", 0),
                                "n_layers": details.get("n_layers", 0),
                                "n_heads": details.get("n_heads", 0),
                                "n_kv_heads": details.get("n_kv_heads", 0),
                                "embedding_dim": details.get("n_embd", 0),
                                "context_length": details.get("n_ctx", 0),
                                "errors": [],
                            })()
                            break
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            # Server not running or API failed, fall back to GGUF parsing
            pass

    # Fall back to GGUF header parsing if API didn't work
    if gguf_info is None and profile.weights_path.exists():
        try:
            gguf_info = read_gguf_header(profile.weights_path)
        except Exception:
            gguf_info = None

    # Run admission check
    admission = None
    if gguf_info and not gguf_info.errors:
        admission = check_admission(profile, gguf_info)

    result = {
        "profile": profile.alias,
        "weights": {
            "path": str(profile.weights_path),
            "size_gb": round(gguf_info.total_bytes / (1024**3), 1) if gguf_info else None,
            "exists": profile.weights_path.exists(),
            "arch": gguf_info.arch if gguf_info else None,
            "params": gguf_info.params if gguf_info else None,
            "n_layers": gguf_info.n_layers if gguf_info else None,
            "context_length": gguf_info.context_length if gguf_info else None,
        },
        "backend": {
            "binary": str(profile.backend_binary),
            "exists": profile.backend_binary.exists(),
        },
        "admission": {
            "admitted": admission.admitted if admission else None,
            "reason": admission.reason if admission else None,
            "weights_gb": round(admission.weights_bytes / (1024**3), 1) if admission else None,
            "kv_cache_gb": round(admission.kv_bytes / (1024**3), 1) if admission else None,
            "compute_gb": round(admission.compute_bytes / (1024**3), 1) if admission else None,
            "reserve_gb": round(admission.reserve_bytes / (1024**3), 2) if admission else None,
            "required_gb": round(admission.required_bytes / (1024**3), 1) if admission else None,
            "available_gb": (
                round(admission.available_bytes / (1024**3), 1)
                if admission and admission.available_bytes else None
            ),
        },
        "requested": {
            "device": profile.device,
            "n_gpu_layers": profile.n_gpu_layers,
            "ctx_size": profile.ctx_size,
            "cache_type_k": profile.cache_type_k,
            "cache_type_v": profile.cache_type_v,
            "flash_attn": profile.flash_attn,
            "threads": profile.threads,
            "port": profile.port,
            "host": profile.host,
        },
        "validation_errors": errors,
        "supported": {
            "device": "unknown",
            "flash_attn": "unknown",
            "speculative_decoding": "unknown",
        },
        "unsupported": [],
        "unknown": ["device_capability", "flash_attn_support", "speculative_decoding"],
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Plan for profile '{profile.alias}':")
        print(f"  Weights: {profile.weights_path}")
        if result["weights"]["size_gb"]:
            print(f"  Weight size: {result['weights']['size_gb']} GB")
        if gguf_info:
            print(f"  Architecture: {gguf_info.arch}")
            print(f"  Parameters: {gguf_info.params}")
            print(f"  Layers: {gguf_info.n_layers}")
            print(f"  Model context: {gguf_info.context_length}")
        print(f"  Backend: {profile.backend_binary}")
        print(f"  Device: {profile.device}")
        print(f"  GPU layers: {profile.n_gpu_layers}")
        print(f"  Context: {profile.ctx_size}")
        print(f"  KV cache: {profile.cache_type_k} (K) / {profile.cache_type_v} (V)")
        print(f"  Flash attention: {profile.flash_attn}")
        print(f"  Threads: {profile.threads}")
        print(f"  Port: {profile.host}:{profile.port}")
        if result["admission"]["admitted"] is not None:
            print(f"  Admission: {'ADMITTED' if result['admission']['admitted'] else 'REJECTED'}")
            print(f"  Reason: {result['admission']['reason']}")
            print(f"  Required: {result['admission']['required_gb']} GB")
            if result["admission"]["available_gb"]:
                print(f"  Available: {result['admission']['available_gb']} GB")
        if errors:
            print("  Validation errors:")
            for err in errors:
                print(f"    - {err}")
        else:
            print("  Validation: OK")

    return 0 if not errors else 1


def cmd_up(args: argparse.Namespace) -> int:
    """Start llama-server for a profile."""
    config = load_config()
    if args.profile not in config.profiles:
        print(f"Error: profile '{args.profile}' not found")
        return 1

    profile = config.profiles[args.profile]
    runtime_dir = config.runtime_dir
    state_dir = config.state_dir

    try:
        with acquire_lock(runtime_dir):
            sup = Supervisor(runtime_dir, state_dir)
            # Check if already running
            existing = sup.status()
            if existing.state in ("READY", "STARTING"):
                print(f"Backend already {existing.state} (instance {existing.instance_uuid})")
                return 0

            print(f"Launching profile '{profile.alias}'...")
            instance = sup.launch(profile, timeout=args.timeout, foreground=args.foreground)

            if instance.state == "READY":
                print(f"Backend READY (instance {instance.instance_uuid}, PID {instance.pid})")
                print(f"  Port: {instance.port}")
                return 0
            else:
                print(f"Launch FAILED: {instance.last_error}")
                return 1
    except TimeoutError:
        print("Error: could not acquire supervisor lock (another cupola operation in progress)")
        return 1


def cmd_down(args: argparse.Namespace) -> int:
    """Stop the running llama-server."""
    config = load_config()
    runtime_dir = config.runtime_dir
    state_dir = config.state_dir

    try:
        with acquire_lock(runtime_dir):
            sup = Supervisor(runtime_dir, state_dir)
            instance = sup.stop(timeout=args.timeout)

            if instance.state == "STOPPED":
                print("Backend stopped.")
                return 0
            elif instance.state == "DEGRADED":
                print(f"WARNING: degraded stop — {instance.last_error}")
                return 1
            else:
                print(f"Backend stopped (was in state: {instance.state}).")
                return 0
    except TimeoutError:
        print("Error: could not acquire supervisor lock")
        return 1


def cmd_status(args: argparse.Namespace) -> int:
    """Show current instance status with telemetry info."""
    config = load_config()
    sup = Supervisor(config.runtime_dir, config.state_dir, config=config)
    instance = sup.status()

    result = instance.to_dict()

    # Add telemetry info if available
    telemetry_path = config.state_dir / "telemetry" / "current.json"
    if telemetry_path.exists():
        try:
            with open(telemetry_path, "r") as f:
                telemetry_data = json.load(f)
            result["telemetry"] = telemetry_data.get("telemetry", {})
        except (OSError, json.JSONDecodeError):
            pass

    # Add RAM disk info if enabled
    if config.ramdisk.enabled and config.ramdisk.path:
        try:
            from pathlib import Path as P
            ramdisk_info = probe_ramdisk(
                P(config.ramdisk.path),
                warn_pct=config.ramdisk.warn_used_pct,
                critical_pct=config.ramdisk.critical_used_pct,
            )
            result["ramdisk"] = ramdisk_info.to_dict()
        except Exception:
            pass

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"State: {instance.state}")
        if instance.instance_uuid:
            print(f"Instance: {instance.instance_uuid}")
        if instance.pid:
            print(f"PID: {instance.pid}")
        if instance.port:
            print(f"Port: {instance.port}")
        if instance.profile:
            print(f"Profile: {instance.profile}")
        if instance.created_at:
            print(f"Started: {instance.created_at}")
        if instance.last_error:
            print(f"Error: {instance.last_error}")

        # Print telemetry summary
        if "telemetry" in result:
            tel = result["telemetry"]
            print(f"  Telemetry quality: {tel.get('quality', 'unknown')}")
            if tel.get("hotspot_celsius") is not None:
                print(f"  Hotspot: {tel['hotspot_celsius']:.1f}°C")
            if tel.get("edge_celsius") is not None:
                print(f"  Edge: {tel['edge_celsius']:.1f}°C")
            if tel.get("power_w") is not None:
                print(f"  Power: {tel['power_w']:.1f} W")
            if tel.get("missing"):
                print(f"  Missing sensors: {', '.join(tel['missing'])}")

        # Print RAM disk info
        if "ramdisk" in result:
            rd = result["ramdisk"]
            if rd.get("exists"):
                if rd.get("used_gb") is not None and rd.get("total_gb") is not None:
                    used = rd["used_gb"]
                    total = rd["total_gb"]
                    pct = rd.get("used_pct", 0)
                    print(f"  RAM disk: {used} GB / {total} GB ({pct:.1f}%)")
                    print(f"    Quality: {rd.get('quality', 'unknown')}")
                    if rd.get("models"):
                        print(f"    Models: {len(rd['models'])}")
            else:
                errors = rd.get("errors", [])
                print(f"  RAM disk: unavailable ({', '.join(errors)})")

    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    """Execute a command in a sandboxed environment."""
    if not args.sandbox:
        print("Error: --sandbox is required for cupola exec")
        return 1

    result = run_sandboxed(
        command=args.command,
        timeout=args.timeout,
        max_memory_mb=args.max_memory_mb,
        cpu_time_seconds=args.cpu_time_seconds,
    )

    if args.json:
        output = {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "killed": result.killed,
            "elapsed_seconds": result.elapsed_seconds,
        }
        print(json.dumps(output, indent=2))
    else:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if result.timed_out:
            print(f"\n[timeout after {args.timeout}s]", file=sys.stderr)
        if result.killed:
            print("\n[killed]", file=sys.stderr)

    return result.exit_code


def cmd_bench(args: argparse.Namespace) -> int:
    """Dispatch a bench subcommand."""
    bench_command = getattr(args, "bench_command", None)
    if bench_command == "lane":
        return cmd_bench_lane(args)
    if bench_command == "system":
        return cmd_bench_system(args)
    if bench_command == "compare":
        return cmd_bench_compare(args)
    if bench_command == "suite":
        return cmd_bench_suite(args)
    if bench_command == "perf":
        return cmd_bench_profile(args)
    print(f"Error: unknown bench subcommand {bench_command!r}",
          file=sys.stderr)
    return 2


def cmd_bench_profile(args: argparse.Namespace) -> int:
    """Benchmark a profile for performance and context size."""
    config = load_config()
    if args.profile not in config.profiles:
        print(f"Error: profile '{args.profile}' not found in configuration")
        print(f"Available profiles: {', '.join(config.profiles.keys())}")
        return 1

    profile = config.profiles[args.profile]

    # Parse context sizes if specified
    context_sizes = None
    if args.context_sizes:
        try:
            context_sizes = [int(s) for s in args.context_sizes.split(",")]
            context_sizes.sort()
        except ValueError:
            print(f"Error: invalid context sizes: {args.context_sizes}")
            return 1

    results = bench_profile(profile, context_sizes)

    if args.json:
        output = {
            "baseline": results["baseline"].to_dict(),
            "context_test": None,
        }
        if results.get("context_test"):
            ct = results["context_test"]
            output["context_test"] = {
                "max_working": ct.max_working,
                "failed_at": ct.failed_at,
                "failed_reason": ct.failed_reason,
                "tests": [t.to_dict() for t in ct.tests],
            }
        print(json.dumps(output, indent=2))
    else:
        print(format_bench_output(results))

    return 0


def cmd_bench_compare(args: argparse.Namespace) -> int:
    """Compare report files, optionally attributing the difference to lanes."""
    from cupola.compare import (
        CompareError,
        attribute_interaction,
        attribute_marginal,
        attribute_total,
        format_attribution,
        load_reports,
    )

    mode = getattr(args, "attribute", None)
    try:
        if mode == "total":
            baseline, full = load_reports([args.baseline, args.candidate])
            attribution = attribute_total(baseline, full)
        elif mode == "marginal":
            baseline = load_reports([args.baseline])[0]
            arms = {}
            for spec in args.arm or []:
                if "=" not in spec:
                    print(f"Error: --arm needs LANE=REPORT, got {spec!r}",
                          file=sys.stderr)
                    return 2
                lane, path = spec.split("=", 1)
                arms[lane] = load_reports([path])[0]
            if not arms:
                print("Error: marginal attribution needs at least one "
                      "--arm LANE=REPORT", file=sys.stderr)
                return 2
            attribution = attribute_marginal(baseline, arms)
        elif mode == "interaction":
            baseline, full = load_reports([args.baseline, args.candidate])
            members = {}
            for spec in args.member or []:
                if "=" not in spec:
                    print(f"Error: --member needs LANE=REPORT, got {spec!r}",
                          file=sys.stderr)
                    return 2
                lane, path = spec.split("=", 1)
                members[lane] = load_reports([path])[0]
            attribution = attribute_interaction(members, group=full)
        else:
            baseline, candidate = load_reports([args.baseline,
                                                args.candidate])
            attribution = attribute_total(baseline, candidate)
    except CompareError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    payload = attribution.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(format_attribution(payload))

    verdicts = {c.verdict for c in attribution.comparisons}
    if "FAIL" in verdicts:
        return 1
    # A run that proves nothing must not exit successfully: INSUFFICIENT_SAMPLE
    # and NON_COMPARABLE are inconclusive, not passing.
    if not attribution.comparisons or verdicts & {"INSUFFICIENT_SAMPLE",
                                                  "NON_COMPARABLE"}:
        return 2
    return 0


def cmd_bench_system(args: argparse.Namespace) -> int:
    """List the systems declared for benchmark runs."""
    from cupola.systems import SystemError, load_systems

    root = Path(args.root) if getattr(args, "root", None) else None
    try:
        systems = load_systems(root)
    except SystemError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"systems": [s.to_dict() for s in systems]},
                         indent=2))
        return 0
    if not systems:
        print("No systems declared (benchmarks/systems.json is missing).")
        return 0
    for system in systems:
        print(f"  {system.name:20s} {system.transport:8s} "
              f"{system.profile or system.base_url or ''}")
        for error in system.validate():
            print(f"      invalid: {error}")
        if system.notes:
            print(f"      {system.notes}")
    return 0


def cmd_bench_lane(args: argparse.Namespace) -> int:
    """Inventory, snapshot, diff or verify toolchain lanes."""
    from cupola.lanes import (
        LaneError,
        collect,
        diff_snapshots,
        read_snapshot,
        write_snapshot,
    )

    lane_command = getattr(args, "lane_command", "")
    if lane_command == "diff":
        try:
            before = read_snapshot(Path(args.before))
            after = read_snapshot(Path(args.after))
        except LaneError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        changes = diff_snapshots(before, after)
        if args.json:
            print(json.dumps({
                "before_hash": before.get("lane_set_hash"),
                "after_hash": after.get("lane_set_hash"),
                "changes": [change.to_dict() for change in changes],
            }, indent=2))
            return 0
        if not changes:
            print("No lane differences.")
            return 0
        print(f"{len(changes)} lane change(s):")
        for change in changes:
            print(f"  {change.change:8s} {change.lane_id}")
            for field, values in sorted(change.fields.items()):
                print(f"      {field}: {values[0]!r} -> {values[1]!r}")
        return 0

    dsh_home = Path(args.dsh_home) if getattr(args, "dsh_home", None) else None
    workspace = Path(args.workspace) if getattr(args, "workspace", None) else None
    kinds = getattr(args, "kind", None)
    try:
        lane_set = collect(args.profile, home=dsh_home, workspace=workspace,
                           dump=not getattr(args, "no_dump", False))
    except LaneError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if kinds:
        wanted = set(kinds)
        lane_set.lanes = [lane for lane in lane_set.lanes
                          if lane.kind in wanted]

    if lane_command == "snapshot":
        out_dir = Path(args.out) if getattr(args, "out", None) else None
        path = write_snapshot(lane_set, out_dir)
        if args.json:
            print(json.dumps({"path": str(path),
                              "lane_set_hash": lane_set.lane_set_hash()},
                             indent=2))
        else:
            print(f"Wrote {path}")
            print(f"lane_set_hash: {lane_set.lane_set_hash()}")
        return 0

    if lane_command == "verify":
        problems = lane_set.warnings()
        payload = {
            "profile": lane_set.profile,
            "lane_set_hash": lane_set.lane_set_hash(),
            "active_lanes": len(lane_set.active()),
            "problems": problems,
            "errors": lane_set.errors,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"Profile: {lane_set.profile}")
            print(f"Active lanes: {len(lane_set.active())} "
                  f"of {len(lane_set.lanes)}")
            for problem in problems:
                print(f"  warn: {problem}")
            for error in lane_set.errors:
                print(f"  error: {error}")
            if not problems and not lane_set.errors:
                print("No lane problems detected.")
        return 0

    if args.json:
        print(json.dumps(lane_set.to_dict(), indent=2))
        return 0
    print(f"Toolchain lanes for profile '{lane_set.profile}' "
          f"(hash {lane_set.lane_set_hash()[:12]}):")
    for lane in lane_set.lanes:
        mark = " " if lane.declared == "active" else "-"
        print(f"  {mark} {lane.kind:12s} {lane.lane_id.split(':', 1)[1]}")
        if lane.detail:
            print(f"      {lane.detail}")
    for problem in lane_set.warnings():
        print(f"  warn: {problem}")
    for error in lane_set.errors:
        print(f"  error: {error}")
    return 0


def cmd_bench_suite(args: argparse.Namespace) -> int:
    """List, validate, plan or execute benchmark suites."""
    from cupola.suites import SuiteError, list_suites, load_suite, plan_run

    suite_command = getattr(args, "suite_command", "")
    root = Path(args.root) if getattr(args, "root", None) else None
    suite_id = getattr(args, "suite", None) or getattr(args, "suite_id", None)

    if suite_command == "list":
        rows = list_suites(root)
        if args.json:
            print(json.dumps({"suites": rows}, indent=2))
            return 0
        if not rows:
            print("No suites found.")
            return 0
        for row in rows:
            print(f"  {row['suite_id']:22s} tasks={row['tasks']:<4} "
                  f"{row['status']}")
        return 0

    try:
        suite = load_suite(suite_id, root)
    except SuiteError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if suite_command == "validate":
        if args.json:
            print(json.dumps({"suite_id": suite.suite_id,
                              "valid": not suite.errors,
                              "errors": suite.errors,
                              "suite_fingerprint": suite.fingerprint()},
                             indent=2))
        elif suite.errors:
            print(f"Suite '{suite.suite_id}' is invalid:")
            for error in suite.errors:
                print(f"  - {error}")
        else:
            print(f"Suite '{suite.suite_id}' is valid "
                  f"({len(suite.tasks)} tasks, "
                  f"fingerprint {suite.fingerprint()[:12]}).")
        return 1 if suite.errors else 0

    if getattr(args, "execute", False):
        return _execute_suite(args, suite)

    plan = plan_run(suite, task_id=getattr(args, "task", None),
                    system=getattr(args, "system", None),
                    runs=getattr(args, "runs", 1))
    if args.json:
        print(json.dumps(plan, indent=2))
        return 0
    print(f"Suite: {suite.suite_id} ({suite.domain}, "
          f"{len(suite.tasks)} tasks)")
    print(f"Fingerprint: {suite.fingerprint()}")
    print(f"System: {plan['system'] or '(unset)'}")
    print(f"Runs per task: {plan['runs']}")
    print(f"Planned tasks: {len(plan['tasks'])}")
    for entry in plan["tasks"]:
        print(f"  {entry['task_id']:28s} {entry['kind']:18s} "
              f"graders={','.join(entry['graders']) or 'none'}")
    for note in plan.get("notes") or []:
        print(f"  note: {note}")
    return 0


def _execute_suite(args: argparse.Namespace, suite) -> int:
    """Run a suite for real, then write a report bound to the lane inventory."""
    from cupola.lanes import LaneError, collect, write_snapshot
    from cupola.report import build_report, format_report
    from cupola.runner import RunnerConfig, RunnerError, preflight, run_suite
    from cupola.systems import SystemError, find_system, lane_profile_for

    system_name = getattr(args, "system", None)
    if not system_name:
        print("Error: --execute requires --system NAME", file=sys.stderr)
        return 2
    root = Path(args.root) if getattr(args, "root", None) else None
    try:
        system = find_system(system_name, root)
    except SystemError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    problems = preflight(system)
    if problems:
        for problem in problems:
            print(f"Error: {problem}", file=sys.stderr)
        return 1

    runner_config = RunnerConfig(
        keep_workspace=getattr(args, "keep_workspace", False))
    try:
        outcomes = run_suite(suite.suite_id, system, root,
                             task_id=getattr(args, "task", None),
                             runs=getattr(args, "runs", 1),
                             config=runner_config)
    except RunnerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Always record the toolchain: a report whose lanes are unknown cannot be
    # reproduced, and a missing lane hash would make two runs look comparable
    # when they may not be.
    lane_set = None
    lane_snapshot = None
    lane_warning = None
    try:
        lane_set = collect(lane_profile_for(system))
        lane_snapshot = write_snapshot(lane_set)
    except LaneError as exc:
        lane_warning = str(exc)

    report = build_report(suite, outcomes, lane_set, systems=[system],
                          lane_snapshot=lane_snapshot)
    if lane_warning:
        report.environment["lanes_unavailable"] = lane_warning

    out = Path(args.out) if getattr(args, "out", None) else (
        runner_config.resolved_state_dir() / "reports"
        / f"{suite.suite_id}-{system.name}.json")
    report.write(out)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(format_report(report))
        print()
        print(f"Report: {out}")
    return 1 if any(not o.passed for o in outcomes) else 0


def cmd_update(args: argparse.Namespace) -> int:
    """Update llama.cpp backend."""
    target_version = None
    recompile = False

    if args.version:
        target_version = args.version
    if args.recompile:
        recompile = True

    result = update_backend(
        target_version=target_version,
        recompile=recompile,
        verbose=not args.json,
    )

    if args.json:
        output = {
            "success": result.success,
            "previous_version": result.previous_version,
            "new_version": result.new_version,
            "target_version": result.target_version,
            "build_time_seconds": result.build_time_seconds,
            "error": result.error,
        }
        print(json.dumps(output, indent=2))
    else:
        print(format_update_result(result))

    return 0 if result.success else 1


def cmd_telemetry(args: argparse.Namespace) -> int:
    """One-shot sensor probe or session summary."""
    config = load_config()

    if args.session:
        # Show last session summary
        sessions_dir = config.state_dir / "telemetry" / "sessions"
        if not sessions_dir.exists():
            print("No session summaries found.")
            return 0

        summaries = list(sessions_dir.glob("*.json"))
        if not summaries:
            print("No session summaries found.")
            return 0

        # Sort by modification time, newest first
        summaries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        latest = summaries[0]

        try:
            with open(latest, "r") as f:
                summary = json.load(f)

            if args.json:
                print(json.dumps(summary, indent=2))
            else:
                print(f"Session Summary ({summary.get('instance_uuid', 'unknown')[:8]}...)")
                print(f"  Profile: {summary.get('profile', 'unknown')}")
                print(f"  Duration: {summary.get('duration_s', 0):.1f}s")
                print(f"  Samples: {summary.get('sample_count', 0)}")

                energy = summary.get("energy", {})
                energy_kwh = energy.get("energy_kwh", "N/A")
                energy_tier = energy.get("tier", "N/A")
                print(f"  Energy: {energy_kwh} kWh (tier {energy_tier})")
                print(f"  Max hotspot: {summary.get('max_hotspot_c', 'N/A')}°C")
                print(f"  Max edge: {summary.get('max_edge_c', 'N/A')}°C")
                print(f"  Avg power: {summary.get('avg_power_w', 'N/A')} W")
                print(f"  Peak power: {summary.get('peak_power_w', 'N/A')} W")

                if summary.get("cost") is not None:
                    print(f"  Cost: {summary['cost']:.2f} {summary.get('currency', '')}")
                if summary.get("co2e_kg") is not None:
                    print(f"  CO2e: {summary['co2e_kg']:.4f} kg")

                if summary.get("missing_sensors"):
                    print(f"  Missing sensors: {', '.join(summary['missing_sensors'])}")
        except (OSError, json.JSONDecodeError) as e:
            print(f"Error reading session summary: {e}")
            return 1

    elif args.gc:
        # Garbage collect old telemetry data
        telemetry_dir = config.state_dir / "telemetry"
        if not telemetry_dir.exists():
            print("No telemetry directory found.")
            return 0

        import time

        now = time.time()
        samples_dir = telemetry_dir / "samples"
        deleted = 0

        if samples_dir.exists():
            for sample_file in samples_dir.glob("*.jsonl"):
                age_days = (now - sample_file.stat().st_mtime) / 86400
                if age_days > 14:
                    sample_file.unlink()
                    deleted += 1

        print(f"Garbage collected {deleted} old sample files.")
        return 0

    else:
        # One-shot sensor probe
        reading = probe_sensors()

        if args.json:
            print(json.dumps(reading.to_dict(), indent=2))
        else:
            print(f"Sentinel sensor probe ({reading.source})")
            print(f"  Quality: {reading.quality}")

            if reading.gpu_edge_c is not None:
                print(f"  GPU edge: {reading.gpu_edge_c:.1f}°C")
            if reading.gpu_hotspot_c is not None:
                print(f"  GPU hotspot: {reading.gpu_hotspot_c:.1f}°C")
            if reading.gpu_mem_c is not None:
                print(f"  GPU memory: {reading.gpu_mem_c:.1f}°C")
            if reading.power_w is not None:
                print(f"  Power: {reading.power_w:.1f} W")
            if reading.fan_rpm is not None:
                print(f"  Fan: {reading.fan_rpm} RPM")
            if reading.vram_used_bytes is not None and reading.vram_total_bytes is not None:
                used_gb = reading.vram_used_bytes / (1024**3)
                total_gb = reading.vram_total_bytes / (1024**3)
                print(f"  VRAM: {used_gb:.1f} GB / {total_gb:.1f} GB")

            if reading.missing:
                print(f"  Missing: {', '.join(reading.missing)}")

    return 0


def cmd_ramdisk_status(args: argparse.Namespace) -> int:
    """Show RAM disk status."""
    config = load_config()

    if not config.ramdisk.path:
        print("Error: RAM disk path not configured")
        return 1

    from pathlib import Path as P
    ramdisk_info = probe_ramdisk(
        P(config.ramdisk.path),
        warn_pct=config.ramdisk.warn_used_pct,
        critical_pct=config.ramdisk.critical_used_pct,
    )

    if args.json:
        print(json.dumps(ramdisk_info.to_dict(), indent=2))
    else:
        print(format_ramdisk_info(ramdisk_info))

    return 0


def cmd_ramdisk_list(args: argparse.Namespace) -> int:
    """List models on RAM disk."""
    config = load_config()

    if not config.ramdisk.path:
        print("Error: RAM disk path not configured")
        return 1

    from pathlib import Path as P
    models = list_models_on_ramdisk(P(config.ramdisk.path))

    if args.json:
        print(json.dumps({"models": models}, indent=2))
    else:
        if models:
            print(f"Models on RAM disk ({len(models)}):")
            for model in models:
                print(f"  - {model}")
        else:
            print("No models on RAM disk")

    return 0


def cmd_ramdisk_up(args: argparse.Namespace) -> int:
    """Copy model to RAM disk."""
    config = load_config()

    if args.profile not in config.profiles:
        print(f"Error: profile '{args.profile}' not found")
        return 1

    profile = config.profiles[args.profile]

    if not config.ramdisk.path:
        print("Error: RAM disk path not configured")
        return 1

    from pathlib import Path as P
    try:
        dest = transfer_to_ramdisk(
            profile.weights_path,
            P(config.ramdisk.path),
        )
        print(f"Model copied to RAM disk: {dest}")
        return 0
    except (FileNotFoundError, ValueError, OSError) as e:
        print(f"Error: {e}")
        return 1


def cmd_ramdisk_down(args: argparse.Namespace) -> int:
    """Remove model from RAM disk."""
    config = load_config()

    if args.profile not in config.profiles:
        print(f"Error: profile '{args.profile}' not found")
        return 1

    profile = config.profiles[args.profile]

    if not config.ramdisk.path:
        print("Error: RAM disk path not configured")
        return 1

    from pathlib import Path as P
    ramdisk_path = P(config.ramdisk.path) / profile.weights_path.name

    try:
        if ramdisk_path.exists():
            remove_from_ramdisk(ramdisk_path)
            print(f"Model removed from RAM disk: {ramdisk_path}")
        else:
            print(f"Model not found on RAM disk: {ramdisk_path}")
        return 0
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1


def cmd_setup(args: argparse.Namespace) -> int:
    """Run interactive setup wizard."""
    return run_setup_wizard()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cupola",
        description="CUPOLA — local inference supervisor for AMD ROCm workstations",
    )
    parser.add_argument("--version", action="version", version=f"cupola {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # doctor
    p_doctor = subparsers.add_parser("doctor", help="Read-only hardware/backend capability report")
    p_doctor.add_argument("--json", action="store_true", help="Output as JSON")
    p_doctor.set_defaults(func=cmd_doctor)

    # validate
    p_validate = subparsers.add_parser("validate", help="Validate a profile specification")
    p_validate.add_argument("profile", help="Profile alias to validate")
    p_validate.add_argument("--json", action="store_true", help="Output as JSON")
    p_validate.set_defaults(func=cmd_validate)

    # plan
    p_plan = subparsers.add_parser("plan", help="Explain requested vs effective settings")
    p_plan.add_argument("profile", help="Profile alias to plan")
    p_plan.add_argument("--json", action="store_true", help="Output as JSON")
    p_plan.set_defaults(func=cmd_plan)

    # up
    p_up = subparsers.add_parser("up", help="Start llama-server for a profile")
    p_up.add_argument("profile", help="Profile alias to start")
    p_up.add_argument("--timeout", type=float, default=30.0, help="Readiness timeout (seconds)")
    p_up.add_argument("--foreground", "-F", action="store_true",
                      help="Run in foreground; attach backend output to terminal")
    p_up.set_defaults(func=cmd_up)

    # down
    p_down = subparsers.add_parser("down", help="Stop the running llama-server")
    p_down.add_argument(
        "--timeout", type=float, default=10.0,
        help="Graceful shutdown timeout (seconds)",
    )
    p_down.set_defaults(func=cmd_down)

    # status
    p_status = subparsers.add_parser("status", help="Show current instance status")
    p_status.add_argument("--json", action="store_true", help="Output as JSON")
    p_status.set_defaults(func=cmd_status)

    # telemetry
    p_telemetry = subparsers.add_parser(
        "telemetry",
        help="One-shot sensor probe or session summary",
    )
    p_telemetry.add_argument("--json", action="store_true", help="Output as JSON")
    p_telemetry.add_argument(
        "--session",
        action="store_true",
        help="Show last session summary instead of live probe",
    )
    p_telemetry.add_argument(
        "--gc",
        action="store_true",
        help="Garbage collect old telemetry data (>14 days)",
    )
    p_telemetry.set_defaults(func=cmd_telemetry)

    # exec
    p_exec = subparsers.add_parser("exec", help="Execute command in sandbox")
    p_exec.add_argument(
        "--sandbox", action="store_true", required=True,
        help="Run in sandboxed environment (required)",
    )
    p_exec.add_argument(
        "--timeout", type=int, default=15,
        help="Wall-clock timeout in seconds (default: 15)",
    )
    p_exec.add_argument(
        "--max-memory-mb", type=int, default=2048,
        help="Maximum virtual memory in MB (default: 2048)",
    )
    p_exec.add_argument(
        "--cpu-time-seconds", type=int, default=15,
        help="Maximum CPU time in seconds (default: 15)",
    )
    p_exec.add_argument("--json", action="store_true", help="Output as JSON")
    p_exec.add_argument("command", nargs=argparse.REMAINDER, help="Command to execute")
    p_exec.set_defaults(func=cmd_exec)

    # bench — `cupola bench <profile>` stays the throughput benchmark (kept
    # working by a shim in _parse_args_with_bench_shim); everything else is a
    # subcommand. The parent takes no positional argument: an optional
    # positional here would swallow the subcommand name.
    p_bench = subparsers.add_parser(
        "bench",
        help="Benchmark a profile, or manage toolchain lanes and task suites",
    )
    bench_sub = p_bench.add_subparsers(dest="bench_command", required=True)

    p_perf = bench_sub.add_parser("perf",
                                  help="Throughput benchmark of a profile")
    p_perf.add_argument("profile", help="Profile alias to benchmark")
    p_perf.add_argument(
        "--context-sizes",
        help="Comma-separated list of context sizes to test "
             "(e.g. 8192,16384,32768)",
    )
    p_perf.add_argument("--json", action="store_true", help="Output as JSON")

    p_lane = bench_sub.add_parser(
        "lane", help="Inventory the installed agent-toolchain lanes")
    lane_sub = p_lane.add_subparsers(dest="lane_command", required=True)

    p_lane_list = lane_sub.add_parser(
        "list", help="List every discovered enhancement and its state")
    p_lane_list.add_argument("--profile", default="web",
                             help="dsh profile to inventory (default: web)")
    p_lane_list.add_argument("--dsh-home", help="Override DSH_HOME")
    p_lane_list.add_argument("--workspace", help="Workspace root to inspect")
    p_lane_list.add_argument("--kind", action="append",
                             help="Only this lane kind (repeatable)")
    p_lane_list.add_argument("--no-dump", action="store_true",
                             help="Skip the composed-tree probe (no dsh run)")
    p_lane_list.add_argument("--json", action="store_true")

    p_lane_snap = lane_sub.add_parser(
        "snapshot", help="Write a lane snapshot for later diffing")
    p_lane_snap.add_argument("--profile", default="web")
    p_lane_snap.add_argument("--dsh-home")
    p_lane_snap.add_argument("--workspace")
    p_lane_snap.add_argument("--out", help="Output directory")
    p_lane_snap.add_argument("--no-dump", action="store_true")
    p_lane_snap.add_argument("--json", action="store_true")

    p_lane_diff = lane_sub.add_parser(
        "diff", help="Show what changed between two lane snapshots")
    p_lane_diff.add_argument("before")
    p_lane_diff.add_argument("after")
    p_lane_diff.add_argument("--json", action="store_true")

    p_lane_verify = lane_sub.add_parser(
        "verify", help="Check that declared lanes actually activate")
    p_lane_verify.add_argument("--profile", default="web")
    p_lane_verify.add_argument("--dsh-home")
    p_lane_verify.add_argument("--workspace")
    p_lane_verify.add_argument("--no-dump", action="store_true")
    p_lane_verify.add_argument("--json", action="store_true")

    p_compare = bench_sub.add_parser(
        "compare", help="Compare reports; attribute the difference to lanes")
    p_compare.add_argument("--baseline", required=True,
                           help="Baseline report JSON")
    p_compare.add_argument("--candidate", required=True,
                           help="Candidate report JSON")
    p_compare.add_argument("--attribute", choices=["total", "marginal",
                                                  "interaction"],
                           help="How to attribute the difference")
    p_compare.add_argument("--arm", action="append",
                           help="LANE=REPORT for marginal attribution")
    p_compare.add_argument("--member", action="append",
                           help="LANE=REPORT for pairwise interaction")
    p_compare.add_argument("--json", action="store_true")

    p_system = bench_sub.add_parser("system", help="Systems under test")
    system_sub = p_system.add_subparsers(dest="system_command", required=True)
    p_system_list = system_sub.add_parser("list", help="List declared systems")
    p_system_list.add_argument("--root", help="Benchmark root directory")
    p_system_list.add_argument("--json", action="store_true")

    p_suite = bench_sub.add_parser("suite", help="Task suites for the harness")
    suite_sub = p_suite.add_subparsers(dest="suite_command", required=True)
    p_suite_list = suite_sub.add_parser("list", help="List discoverable suites")
    p_suite_list.add_argument("--root", help="Benchmark root directory")
    p_suite_list.add_argument("--json", action="store_true")
    p_suite_validate = suite_sub.add_parser("validate",
                                            help="Validate a suite manifest")
    p_suite_validate.add_argument("suite_id")
    p_suite_validate.add_argument("--root")
    p_suite_validate.add_argument("--json", action="store_true")
    p_suite_run = suite_sub.add_parser(
        "run", help="Plan (dry-run) or execute a suite")
    p_suite_run.add_argument("--suite", required=True)
    p_suite_run.add_argument("--root")
    p_suite_run.add_argument("--system", help="System under test name")
    p_suite_run.add_argument("--task", help="One task (default: all)")
    p_suite_run.add_argument("--runs", type=int, default=1)
    p_suite_run.add_argument("--dry-run", action="store_true",
                             help="Validate and print the plan; run nothing")
    p_suite_run.add_argument("--execute", action="store_true",
                             help="Actually run the tasks (default: dry-run)")
    p_suite_run.add_argument("--out", help="Where to write the run report")
    p_suite_run.add_argument("--keep-workspace", action="store_true",
                             help="Keep each task workspace for inspection")
    p_suite_run.add_argument("--json", action="store_true")

    p_bench.set_defaults(func=cmd_bench)

    # update
    p_update = subparsers.add_parser("update", help="Update llama.cpp backend")
    p_update.add_argument(
        "--backend",
        action="store_true",
        help="Update llama.cpp backend",
    )
    p_update.add_argument(
        "version",
        nargs="?",
        help="Specific version to update to (default: latest)",
    )
    p_update.add_argument(
        "--recompile",
        action="store_true",
        help="Force recompile even if already at target version",
    )
    p_update.add_argument("--json", action="store_true", help="Output as JSON")
    p_update.set_defaults(func=cmd_update)

    # setup
    p_setup = subparsers.add_parser("setup", help="Interactive setup wizard")
    p_setup.set_defaults(func=cmd_setup)

    # ramdisk
    p_ramdisk = subparsers.add_parser("ramdisk", help="RAM disk management")
    ramdisk_subparsers = p_ramdisk.add_subparsers(dest="ramdisk_command", required=True)

    p_rd_status = ramdisk_subparsers.add_parser("status", help="Show RAM disk status")
    p_rd_status.add_argument("--json", action="store_true", help="Output as JSON")
    p_rd_status.set_defaults(func=cmd_ramdisk_status)

    p_rd_list = ramdisk_subparsers.add_parser("list", help="List models on RAM disk")
    p_rd_list.add_argument("--json", action="store_true", help="Output as JSON")
    p_rd_list.set_defaults(func=cmd_ramdisk_list)

    p_rd_up = ramdisk_subparsers.add_parser("up", help="Copy model to RAM disk")
    p_rd_up.add_argument("profile", help="Profile alias")
    p_rd_up.set_defaults(func=cmd_ramdisk_up)

    p_rd_down = ramdisk_subparsers.add_parser("down", help="Remove model from RAM disk")
    p_rd_down.add_argument("profile", help="Profile alias")
    p_rd_down.set_defaults(func=cmd_ramdisk_down)

    args = _parse_args_with_bench_shim(parser, argv)
    return args.func(args)


def _parse_args_with_bench_shim(parser: argparse.ArgumentParser,
                                argv: list[str] | None):
    """Parse argv, keeping the historical ``cupola bench <profile>`` form.

    ``bench`` routes through subcommands, but the documented form has always
    been ``cupola bench coding``. A bare profile alias is rewritten to the
    explicit ``bench perf <profile>`` before parsing.
    """
    tokens = list(sys.argv[1:] if argv is None else argv)
    if "bench" in tokens:
        index = tokens.index("bench")
        rest = tokens[index + 1:]
        known = {"perf", "lane", "suite", "system", "compare"}
        if rest and not rest[0].startswith("-") \
                and not any(token in known for token in rest):
            tokens = tokens[:index + 1] + ["perf"] + rest
    return parser.parse_args(tokens)


if __name__ == "__main__":
    sys.exit(main())
