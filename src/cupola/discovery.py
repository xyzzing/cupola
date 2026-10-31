"""Executable and artifact discovery for Cupola.

Centralizes all discovery of llama.cpp binaries and source.
No other module should embed developer-machine paths.

Discovery precedence:
1. Explicit profile/config value
2. Environment variable override
3. PATH discovery (shutil.which)
4. Generic system locations
5. Return None with targeted message
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional


def resolve_path_from_env(variable: str) -> Optional[Path]:
    """Resolve a path from an environment variable.

    Validates that the path exists and is absolute.
    """
    value = os.environ.get(variable)
    if not value:
        return None

    path = Path(value)
    if not path.is_absolute():
        raise ValueError(
            f"{variable} must be an absolute path, got: {value}"
        )
    if not path.exists():
        raise ValueError(
            f"{variable} path does not exist: {value}"
        )
    return path


def find_llama_server(explicit: Optional[Path] = None) -> Optional[Path]:
    """Find the llama-server binary.

    Precedence:
    1. Explicit path from profile
    2. CUPOLA_LLAMA_SERVER environment variable
    3. PATH discovery
    4. Generic system locations
    """
    # 1. Explicit profile value
    if explicit is not None:
        if not explicit.exists():
            raise ValueError(
                f"Profile backend_binary does not exist: {explicit}"
            )
        if not explicit.is_file():
            raise ValueError(
                f"Profile backend_binary is not a file: {explicit}"
            )
        return explicit

    # 2. Environment override
    env_path = resolve_path_from_env("CUPOLA_LLAMA_SERVER")
    if env_path is not None:
        if not env_path.is_file():
            raise ValueError(
                f"CUPOLA_LLAMA_SERVER is not a file: {env_path}"
            )
        if not os.access(env_path, os.X_OK):
            raise ValueError(
                f"CUPOLA_LLAMA_SERVER is not executable: {env_path}"
            )
        return env_path

    # 3. PATH discovery
    found = shutil.which("llama-server")
    if found:
        return Path(found)

    # 4. Generic system locations
    system_paths = [
        "/usr/local/bin/llama-server",
        "/usr/bin/llama-server",
        "/opt/llama.cpp/bin/llama-server",
    ]
    for path in system_paths:
        if Path(path).exists():
            return Path(path)

    # 5. Not found
    return None


def find_llama_bench(explicit: Optional[Path] = None) -> Optional[Path]:
    """Find the llama-bench binary."""
    if explicit is not None:
        if not explicit.exists():
            raise ValueError(
                f"Profile bench_binary does not exist: {explicit}"
            )
        return explicit

    env_path = resolve_path_from_env("CUPOLA_LLAMA_BENCH")
    if env_path is not None:
        return env_path

    found = shutil.which("llama-bench")
    if found:
        return Path(found)

    system_paths = [
        "/usr/local/bin/llama-bench",
        "/usr/bin/llama-bench",
        "/opt/llama.cpp/bin/llama-bench",
    ]
    for path in system_paths:
        if Path(path).exists():
            return Path(path)

    return None


def find_llama_cpp_source(explicit: Optional[Path] = None) -> Optional[Path]:
    """Find the llama.cpp source directory."""
    if explicit is not None:
        if not explicit.exists():
            raise ValueError(
                f"Profile source_path does not exist: {explicit}"
            )
        if not explicit.is_dir():
            raise ValueError(
                f"Profile source_path is not a directory: {explicit}"
            )
        return explicit

    env_path = resolve_path_from_env("CUPOLA_LLAMA_CPP_SOURCE")
    if env_path is not None:
        if not env_path.is_dir():
            raise ValueError(
                f"CUPOLA_LLAMA_CPP_SOURCE is not a directory: {env_path}"
            )
        return env_path

    system_paths = [
        "/usr/local/src/llama.cpp",
        "/opt/llama.cpp",
    ]
    for path in system_paths:
        if Path(path).exists() and Path(path).is_dir():
            return Path(path)

    return None


def find_weights(explicit: Optional[Path] = None) -> Optional[Path]:
    """Find model weights (GGUF file)."""
    if explicit is not None:
        if not explicit.exists():
            raise ValueError(
                f"Profile weights_path does not exist: {explicit}"
            )
        return explicit

    env_path = resolve_path_from_env("CUPOLA_WEIGHTS")
    if env_path is not None:
        return env_path

    return None


def get_discovery_source(
    explicit: Optional[Path],
    found: Optional[Path],
    env_var: str,
) -> str:
    """Describe how a path was discovered."""
    if explicit is not None:
        return "profile"
    if os.environ.get(env_var):
        return env_var
    if shutil.which("llama-server") or shutil.which("llama-bench"):
        return "PATH"
    return "system path"