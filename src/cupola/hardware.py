"""Read-only hardware probing for cupola doctor."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class HardwareInfo:
    kernel: str
    os_release: str
    cpu_model: str
    cpu_cores: int
    cpu_threads: int
    ram_total_gb: float
    ram_available_gb: float
    gpu_name: Optional[str]
    gpu_pci: Optional[str]
    gpu_vram_total_gb: Optional[float]
    gpu_vram_used_gb: Optional[float]
    rocm_version: Optional[str]
    llama_server_version: Optional[str]
    llama_server_running: bool
    llama_server_port: Optional[int]
    errors: list[str]


def run_cmd(cmd: list[str], timeout: float = 5.0) -> Optional[str]:
    """Run a command and return stdout, or None on failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def get_kernel() -> str:
    try:
        with open("/proc/sys/kernel/osrelease") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def get_os_release() -> str:
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "unknown"


def get_cpu_info() -> tuple[str, int, int]:
    model = "unknown"
    cores = 0
    threads = 0
    try:
        with open("/proc/cpuinfo") as f:
            content = f.read()
        for line in content.splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
            elif line.startswith("processor"):
                threads += 1
        # Count cores from lscpu
        lscpu = run_cmd(["lscpu"])
        if lscpu:
            for line in lscpu.splitlines():
                if line.startswith("Core(s) per socket"):
                    try:
                        cores_per_socket = int(line.split(":", 1)[1].strip())
                        sockets = 1
                        for l2 in lscpu.splitlines():
                            if l2.startswith("Socket(s)"):
                                sockets = int(l2.split(":", 1)[1].strip())
                                break
                        cores = cores_per_socket * sockets
                    except ValueError:
                        pass
    except OSError:
        pass
    return model, cores, threads


def get_ram_info() -> tuple[float, float]:
    total_mb = 0
    available_mb = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total_mb = int(line.split()[1]) / 1024
                elif line.startswith("MemAvailable:"):
                    available_mb = int(line.split()[1]) / 1024
    except OSError:
        pass
    return total_mb / 1024, available_mb / 1024


def get_gpu_info() -> tuple[Optional[str], Optional[str]]:
    """Return (gpu_name, gpu_pci_identity)."""
    lspci = run_cmd(["lspci", "-nn"])
    if not lspci:
        return None, None
    for line in lspci.splitlines():
        if "VGA compatible controller" in line:
            # Extract name and PCI ID
            parts = line.split(": ", 2)
            if len(parts) >= 3:
                # parts[2] is like "Advanced Micro Devices, Inc. [AMD/ATI] Navi 31"
                # "[Radeon RX 7900 XT/7900 XTX/7900 GRE/7900M] [1002:744c] (rev c8)"
                name = parts[2].split(" [")[0]
                pci_id = parts[2].split("[")[-1].rstrip("] (rev c8)").rstrip("]")
                return name, pci_id
    return None, None


def get_rocm_version() -> Optional[str]:
    """Get ROCm version from rpm or package manager."""
    rpm = run_cmd(["rpm", "-qa", "--queryformat", "%{NAME}-%{VERSION}\n"])
    if rpm:
        for line in rpm.splitlines():
            if line.startswith("rocm-runtime-"):
                parts = line.split("-")
                if len(parts) >= 3:
                    return parts[2]
    return None


def get_vram_info() -> tuple[Optional[float], Optional[float]]:
    """Get VRAM total and used in GB via rocm-smi."""
    smi = run_cmd(["rocm-smi", "--showmeminfo", "vram"])
    if not smi:
        return None, None
    total_bytes = None
    used_bytes = None
    for line in smi.splitlines():
        if "VRAM Total Memory" in line:
            try:
                total_bytes = int(line.split(":")[-1].strip())
            except ValueError:
                pass
        if "VRAM Total Used Memory" in line:
            try:
                used_bytes = int(line.split(":")[-1].strip())
            except ValueError:
                pass
    total_gb = total_bytes / (1024**3) if total_bytes else None
    used_gb = used_bytes / (1024**3) if used_bytes else None
    return total_gb, used_gb


def check_llama_server() -> tuple[bool, Optional[int], Optional[str]]:
    """Check if llama-server is running. Return (running, port, version)."""
    # Check for running process
    ps = run_cmd(["ps", "aux"])
    if not ps:
        return False, None, None

    running = False
    port = None
    for line in ps.splitlines():
        if "llama-server" in line and "grep" not in line:
            running = True
            # Try to extract port from environment
            break

    # Check port 8080 via /proc/net/tcp or ss
    if running:
        ss = run_cmd(["ss", "-tlnp"])
        if ss:
            for line in ss.splitlines():
                if "llama-server" in line:
                    # Parse port from line like "0.0.0.0:8080"
                    try:
                        addr_part = line.split()[3]
                        port = int(addr_part.split(":")[-1])
                    except (ValueError, IndexError):
                        pass
                    break

    # Get version
    version = None
    from cupola.discovery import find_llama_server
    which = find_llama_server()
    if which is None:
        pass
    version_out = run_cmd([str(which), "--version"]) if which else None
    if version_out:
        version = version_out.splitlines()[0]

    return running, port, version


def probe() -> HardwareInfo:
    """Run all hardware probes and return aggregated info."""
    errors = []

    kernel = get_kernel()
    os_release = get_os_release()
    cpu_model, cpu_cores, cpu_threads = get_cpu_info()
    ram_total, ram_available = get_ram_info()
    gpu_name, gpu_pci = get_gpu_info()
    rocm_version = get_rocm_version()
    vram_total, vram_used = get_vram_info()
    llama_running, llama_port, llama_version = check_llama_server()

    if not gpu_name:
        errors.append("No GPU detected via lspci")
    if not rocm_version:
        errors.append("ROCm version not detected")
    if not llama_running:
        errors.append("llama-server not running")

    return HardwareInfo(
        kernel=kernel,
        os_release=os_release,
        cpu_model=cpu_model,
        cpu_cores=cpu_cores,
        cpu_threads=cpu_threads,
        ram_total_gb=ram_total,
        ram_available_gb=ram_available,
        gpu_name=gpu_name,
        gpu_pci=gpu_pci,
        gpu_vram_total_gb=vram_total,
        gpu_vram_used_gb=vram_used,
        rocm_version=rocm_version,
        llama_server_version=llama_version,
        llama_server_running=llama_running,
        llama_server_port=llama_port,
        errors=errors,
    )
