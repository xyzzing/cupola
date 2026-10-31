"""CUPOLA configuration schema and loading."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cupola.discovery import find_llama_server

# Support CUPOLA_CONFIG_DIR override for CI and isolated testing
_config_dir_env = os.environ.get("CUPOLA_CONFIG_DIR")
if _config_dir_env:
    DEFAULT_CONFIG_DIR = Path(_config_dir_env)
else:
    DEFAULT_CONFIG_DIR = Path.home() / ".config" / "cupola"

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "cupola"
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "cupola"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "cupola"

# XDG_RUNTIME_DIR takes priority; fall back to /run/user/$UID/cupola
_xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
if _xdg_runtime:
    DEFAULT_RUNTIME_DIR = Path(_xdg_runtime) / "cupola"
else:
    DEFAULT_RUNTIME_DIR = Path(f"/run/user/{os.getuid()}/cupola")


@dataclass
class ProfileSpec:
    """A single model profile specification."""
    alias: str
    weights_path: Path
    weights_digest: Optional[str] = None  # SHA-256
    backend_binary: Optional[Path] = None  # resolved via discovery if None
    backend_version: Optional[str] = None
    device: str = "ROCm0"
    n_gpu_layers: int = 63
    ctx_size: int = 131072
    cache_type_k: str = "q4_0"
    cache_type_v: str = "q4_0"
    flash_attn: bool = True
    batch_size: int = 4096
    ubatch_size: int = 1024
    threads: int = 6
    n_parallel: int = 1
    port: int = 8080
    host: str = "127.0.0.1"  # loopback by default for privacy
    chat_template: Optional[Path] = None
    max_context: Optional[int] = None
    reserve_bytes: int = 512 * 1024 * 1024  # 512 MiB safety reserve


@dataclass
class SentinelConfig:
    """Sentinel telemetry configuration."""
    sample_hz: float = 1.0
    warn_hotspot_c: float = 95.0
    critical_hotspot_c: float = 105.0
    critical_hold_s: int = 8
    auto_stop_on_critical: bool = False
    assume_power_w: Optional[float] = None
    wall_energy_path: Optional[str] = None


@dataclass
class AccountingConfig:
    """Local operational accounting configuration (estimates only)."""
    currency: str = "USD"
    electricity_tariff_per_kwh: Optional[float] = None
    emissions_region: str = ""
    emissions_factor_kgco2e_per_kwh: Optional[float] = None
    emissions_factor_source: str = ""
    emissions_factor_version: str = ""
    allow_assumed_power: bool = False


@dataclass
class RamdiskConfig:
    """RAM disk configuration for model storage."""
    path: Optional[str] = "/mnt/ai_ramdisk"
    enabled: bool = True
    warn_used_pct: float = 80.0
    critical_used_pct: float = 90.0


@dataclass
class CupolaConfig:
    """Top-level CUPOLA configuration."""
    profiles: dict[str, ProfileSpec] = field(default_factory=dict)
    default_profile: str = "coding"
    config_dir: Path = DEFAULT_CONFIG_DIR
    runtime_dir: Path = DEFAULT_RUNTIME_DIR
    state_dir: Path = DEFAULT_STATE_DIR
    data_dir: Path = DEFAULT_DATA_DIR
    cache_dir: Path = DEFAULT_CACHE_DIR
    sentinel: SentinelConfig = field(default_factory=SentinelConfig)
    accounting: AccountingConfig = field(default_factory=AccountingConfig)
    ramdisk: RamdiskConfig = field(default_factory=RamdiskConfig)


def load_config(config_path: Optional[Path] = None) -> CupolaConfig:
    """Load configuration from TOML file.

    Resolution order:
    1. Explicit config_path argument
    2. XDG_CONFIG_HOME/cupola/config.toml
    3. ~/.config/cupola/config.toml
    """
    if config_path is None:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            config_path = Path(xdg) / "cupola" / "config.toml"
        else:
            config_path = DEFAULT_CONFIG_DIR / "config.toml"

    if not config_path.exists():
        return CupolaConfig()

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    config = CupolaConfig()
    config.config_dir = config_path.parent

    # Parse profiles
    for alias, spec in raw.get("profiles", {}).items():
        profile = ProfileSpec(
            alias=alias,
            weights_path=Path(spec["weights_path"]),
            weights_digest=spec.get("weights_digest"),
            backend_binary=Path(spec["backend_binary"]) if spec.get("backend_binary") else None,
            backend_version=spec.get("backend_version"),
            device=spec.get("device", ProfileSpec.device),
            n_gpu_layers=int(spec.get("n_gpu_layers", ProfileSpec.n_gpu_layers)),
            ctx_size=int(spec.get("ctx_size", ProfileSpec.ctx_size)),
            cache_type_k=spec.get("cache_type_k", ProfileSpec.cache_type_k),
            cache_type_v=spec.get("cache_type_v", ProfileSpec.cache_type_v),
            flash_attn=bool(spec.get("flash_attn", ProfileSpec.flash_attn)),
            batch_size=int(spec.get("batch_size", ProfileSpec.batch_size)),
            ubatch_size=int(spec.get("ubatch_size", ProfileSpec.ubatch_size)),
            threads=int(spec.get("threads", ProfileSpec.threads)),
            n_parallel=int(spec.get("n_parallel", ProfileSpec.n_parallel)),
            port=int(spec.get("port", ProfileSpec.port)),
            host=spec.get("host", ProfileSpec.host),
        )
        if spec.get("chat_template"):
            profile.chat_template = Path(spec["chat_template"])
        config.profiles[alias] = profile

    config.default_profile = raw.get("default_profile", config.default_profile)

    # Parse sentinel configuration
    sentinel_raw = raw.get("sentinel", {})
    if sentinel_raw:
        s = config.sentinel
        config.sentinel = SentinelConfig(
            sample_hz=float(sentinel_raw.get("sample_hz", s.sample_hz)),
            warn_hotspot_c=float(sentinel_raw.get("warn_hotspot_c", s.warn_hotspot_c)),
            critical_hotspot_c=float(sentinel_raw.get("critical_hotspot_c", s.critical_hotspot_c)),
            critical_hold_s=int(sentinel_raw.get("critical_hold_s", s.critical_hold_s)),
            auto_stop_on_critical=bool(
                sentinel_raw.get("auto_stop_on_critical", s.auto_stop_on_critical)
            ),
            assume_power_w=(
                float(sentinel_raw["assume_power_w"])
                if sentinel_raw.get("assume_power_w")
                else None
            ),
            wall_energy_path=sentinel_raw.get("wall_energy_path"),
        )

    # Parse accounting configuration
    accounting_raw = raw.get("accounting", {})
    if accounting_raw:
        a = config.accounting
        config.accounting = AccountingConfig(
            currency=accounting_raw.get("currency", a.currency),
            electricity_tariff_per_kwh=(
                float(accounting_raw["electricity_tariff_per_kwh"])
                if accounting_raw.get("electricity_tariff_per_kwh")
                else None
            ),
            emissions_region=accounting_raw.get("emissions_region", a.emissions_region),
            emissions_factor_kgco2e_per_kwh=(
                float(accounting_raw["emissions_factor_kgco2e_per_kwh"])
                if accounting_raw.get("emissions_factor_kgco2e_per_kwh")
                else None
            ),
            emissions_factor_source=accounting_raw.get(
                "emissions_factor_source", a.emissions_factor_source
            ),
            emissions_factor_version=accounting_raw.get(
                "emissions_factor_version", a.emissions_factor_version
            ),
            allow_assumed_power=bool(
                accounting_raw.get("allow_assumed_power", a.allow_assumed_power)
            ),
        )

    # Parse ramdisk configuration
    ramdisk_raw = raw.get("ramdisk", {})
    if ramdisk_raw:
        r = config.ramdisk
        config.ramdisk = RamdiskConfig(
            path=ramdisk_raw.get("path", r.path),
            enabled=bool(ramdisk_raw.get("enabled", r.enabled)),
            warn_used_pct=float(ramdisk_raw.get("warn_used_pct", r.warn_used_pct)),
            critical_used_pct=float(
                ramdisk_raw.get("critical_used_pct", r.critical_used_pct)
            ),
        )

    # Support CUPOLA_RAMDISK_PATH environment variable override
    env_ramdisk = os.environ.get("CUPOLA_RAMDISK_PATH")
    if env_ramdisk:
        config.ramdisk.path = env_ramdisk

    return config


def save_config(config: CupolaConfig) -> None:
    """Save configuration to TOML file."""
    config_dir = config.config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"

    # Build TOML content
    lines = []
    lines.append(f"default_profile = \"{config.default_profile}\"")
    lines.append("")

    for alias, profile in config.profiles.items():
        lines.append(f"[profiles.{alias}]")
        lines.append(f"weights_path = \"{profile.weights_path}\"")
        if profile.weights_digest:
            lines.append(f"weights_digest = \"{profile.weights_digest}\"")
        if profile.backend_binary:
            lines.append(f"backend_binary = \"{profile.backend_binary}\"")
        lines.append(f"n_gpu_layers = {profile.n_gpu_layers}")
        lines.append(f"ctx_size = {profile.ctx_size}")
        lines.append(f"cache_type_k = \"{profile.cache_type_k}\"")
        lines.append(f"cache_type_v = \"{profile.cache_type_v}\"")
        lines.append(f"flash_attn = {str(profile.flash_attn).lower()}")
        lines.append(f"batch_size = {profile.batch_size}")
        lines.append(f"ubatch_size = {profile.ubatch_size}")
        lines.append(f"threads = {profile.threads}")
        lines.append(f"n_parallel = {profile.n_parallel}")
        lines.append(f"port = {profile.port}")
        lines.append(f"host = \"{profile.host}\"")
        if profile.chat_template:
            lines.append(f"chat_template = \"{profile.chat_template}\"")
        lines.append("")

    with open(config_path, "w") as f:
        f.write("\n".join(lines))


def compute_sha256(path: Path) -> str:
    """Compute SHA256 hash of a file using bounded chunked reads."""
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(64 * 1024 * 1024)  # 64 MiB chunks
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()


def validate_profile(profile: ProfileSpec) -> list[str]:
    """Validate a profile spec. Returns list of error strings (empty = valid)."""
    errors = []

    if not profile.alias:
        errors.append("profile alias is required")

    if not profile.weights_path.exists():
        errors.append(f"weights file not found: {profile.weights_path}")
    elif not profile.weights_path.is_file():
        errors.append(f"weights path is not a file: {profile.weights_path}")
    else:
        # Reject symlink escapes from managed asset paths
        try:
            real = profile.weights_path.resolve()
            if profile.weights_path.parent.resolve() not in real.parents:
                errors.append(f"weights path is a symlink escape: {profile.weights_path}")
        except OSError:
            pass

        # Validate SHA256 hash if specified
        if profile.weights_digest:
            try:
                actual = compute_sha256(profile.weights_path)
                expected = profile.weights_digest.lower()
                if actual != expected:
                    errors.append(
                        f"weights SHA256 mismatch: expected {expected}, "
                        f"got {actual}"
                    )
            except OSError as e:
                errors.append(f"failed to compute weights SHA256: {e}")

    try:
        resolved_binary = find_llama_server(profile.backend_binary)
    except ValueError as e:
        errors.append(f"backend binary error: {e}")
        resolved_binary = None
    if resolved_binary is None:
        errors.append("backend binary not found. Set backend_binary in profile, set CUPOLA_LLAMA_SERVER, or put llama-server on PATH")  # noqa: E501

    if profile.n_gpu_layers < 0:
        errors.append(f"n_gpu_layers must be >= 0, got {profile.n_gpu_layers}")

    if profile.ctx_size <= 0:
        errors.append(f"ctx_size must be > 0, got {profile.ctx_size}")

    if profile.batch_size <= 0:
        errors.append(f"batch_size must be > 0, got {profile.batch_size}")

    if profile.threads < 1:
        errors.append(f"threads must be >= 1, got {profile.threads}")

    if profile.port < 1 or profile.port > 65535:
        errors.append(f"port must be 1-65535, got {profile.port}")

    # Validate host is loopback
    try:
        addr = ipaddress.ip_address(profile.host)
        if not addr.is_loopback:
            errors.append(f"host must be loopback, got {profile.host}")
    except ValueError:
        # Not an IP address — could be hostname
        if profile.host not in ("localhost", "127.0.0.1", "::1"):
            errors.append(f"host must resolve to loopback, got {profile.host}")

    # Validate KV cache types
    valid_cache_types = {"f16", "f32", "q4_0", "q4_1", "q5_0", "q5_1", "q8_0"}
    if profile.cache_type_k not in valid_cache_types:
        errors.append(f"invalid cache_type_k: {profile.cache_type_k}")
    if profile.cache_type_v not in valid_cache_types:
        errors.append(f"invalid cache_type_v: {profile.cache_type_v}")

    return errors