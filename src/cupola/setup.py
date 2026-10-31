"""Cupola interactive setup wizard.

Menu-based CLI for installing and integrating llama.cpp with Cupola,
detecting existing installations, and configuring profiles.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cupola.config import ProfileSpec, load_config, save_config
from cupola.discovery import find_llama_server
from cupola.llama_version import LlamaVersionManager


@dataclass
class InstallationInfo:
    """Detected installation information."""
    llama_server_binary: Optional[Path] = None
    llama_server_version: Optional[str] = None
    systemd_service: Optional[str] = None
    running_process: Optional[dict] = None
    model_files: list[Path] = field(default_factory=list)
    config_file: Optional[Path] = None
    rocm_installed: bool = False
    rocm_version: Optional[str] = None
    gpu_detected: bool = False
    gpu_name: Optional[str] = None


def clear_screen() -> None:
    """Clear terminal screen."""
    os.system("clear")


def print_banner() -> None:
    """Print Cupola setup banner."""
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║              CUPOLA INTERACTIVE SETUP WIZARD               ║")
    print("║         Local 27B LLM Inference on 24GB GPU               ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()


def detect_installation() -> InstallationInfo:
    """Detect existing llama.cpp installation and configuration."""
    info = InstallationInfo()

    # Check for existing Cupola config
    config = load_config()
    if config.config_dir.exists():
        info.config_file = config.config_dir / "config.toml"

    # Search for llama-server binary using discovery
    info.llama_server_binary = find_llama_server()


    # Try to get version
    if info.llama_server_binary:
        try:
            result = subprocess.run(
                [str(info.llama_server_binary), "--version"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                info.llama_server_version = result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Detect ROCm installation
    rocm_paths = ["/opt/rocm", "/usr/lib/rocm"]
    for rocm_path in rocm_paths:
        if Path(rocm_path).exists():
            info.rocm_installed = True
            # Try to get ROCm version
            try:
                result = subprocess.run(
                    ["rocm-smi", "--version"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    info.rocm_version = result.stdout.strip().split("\n")[0]
            except (subprocess.TimeoutExpired, OSError):
                pass
            break

    # Detect GPU
    try:
        result = subprocess.run(
            ["lspci", "-nn"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "VGA compatible controller" in line:
                    info.gpu_detected = True
                    # Extract GPU name
                    parts = line.split(":")
                    if len(parts) >= 3:
                        info.gpu_name = parts[2].strip().split(" ")[0]
                    break
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Check for systemd service
    try:
        result = subprocess.run(
            ["systemctl", "--user", "list-units", "--type=service", "--all"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if "llama" in line.lower() and "service" in line.lower():
                # Extract service name
                parts = line.split()
                if parts:
                    info.systemd_service = parts[0]
                    break
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Check for running llama-server processes
    try:
        result = subprocess.run(
            ["pgrep", "-a", "llama-server"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    info.running_process = {
                        "pid": int(parts[0]),
                        "cmdline": parts[1],
                    }
                    break
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Search for model files
    model_dirs = [
        Path.home() / ".local/share/cupola/models",
        Path.home() / "models",
        Path.home() / ".cache/cupola/models",
    ]
    for model_dir in model_dirs:
        if model_dir.exists():
            for f in model_dir.rglob("*.gguf"):
                info.model_files.append(f)
            for f in model_dir.rglob("*.bin"):
                info.model_files.append(f)

    return info


def install_rocm() -> bool:
    """Install ROCm drivers and libraries."""
    print("Installing ROCm...")
    print("This requires root privileges and may take several minutes.")
    print()

    # Detect distribution
    distro = "unknown"
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("ID="):
                    distro = line.split("=")[1].strip()
                    break
    except OSError:
        pass

    if distro == "fedora":
        print("Installing ROCm on Fedora...")
        try:
            subprocess.run(
                ["sudo", "dnf", "install", "-y", "rocm", "rocm-cl", "hsa-rocr-dev"],
                check=True
            )
        except subprocess.CalledProcessError as e:
            print(f"Failed to install ROCm: {e}")
            print("Try manually: sudo dnf install rocm")
            return False
    elif distro == "ubuntu" or distro == "debian":
        print("Installing ROCm on Ubuntu/Debian...")
        try:
            subprocess.run(
                ["sudo", "apt-get", "update"],
                check=True
            )
            subprocess.run(
                ["sudo", "apt-get", "install", "-y", "rocm-dev"],
                check=True
            )
        except subprocess.CalledProcessError as e:
            print(f"Failed to install ROCm: {e}")
            print("Try manually: sudo apt-get install rocm-dev")
            return False
    else:
        print(f"Unknown distribution: {distro}")
        print("Please install ROCm manually:")
        print("  https://rocm.docs.amd.com/en/latest/install/install.html")
        return False

    # Verify installation
    if shutil.which("rocm-smi"):
        print("ROCm installed successfully!")
        return True
    else:
        print("ROCm installation may have failed. Check manually.")
        return False


def install_llama_cpp(rocm=True) -> bool:
    """Install llama.cpp from source with optional ROCm support."""
    print("Installing llama.cpp from source...")
    print("This may take several minutes.")
    print()

    # Check for dependencies
    deps = ["git", "cmake", "make", "g++"]
    missing = []
    for dep in deps:
        if shutil.which(dep) is None:
            missing.append(dep)

    if missing:
        print(f"Missing dependencies: {', '.join(missing)}")
        print("Please install them first:")
        print(f"  sudo dnf install {' '.join(missing)}")
        print()
        return False

    # Clone repo
    repo_dir = Path.home() / "llama.cpp"
    if not repo_dir.exists():
        print(f"Cloning llama.cpp to {repo_dir}...")
        try:
            subprocess.run(
                ["git", "clone", "https://github.com/ggerganov/llama.cpp.git", str(repo_dir)],
                check=True
            )
        except subprocess.CalledProcessError as e:
            print(f"Failed to clone repo: {e}")
            return False

    # Build
    print("Building llama.cpp...")
    build_dir = repo_dir / "build"
    build_dir.mkdir(exist_ok=True)

    cmake_args = [
        "cmake", "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release"
    ]

    if rocm:
        print("  Enabling ROCm support...")
        cmake_args.extend([
            "-DGGML_HIPBLAS=ON",
            "-DGGML_CUDA=OFF",
        ])

    try:
        subprocess.run(cmake_args, cwd=repo_dir, check=True)
        subprocess.run(
            ["cmake", "--build", str(build_dir), "-j", str(os.cpu_count())],
            cwd=repo_dir, check=True
        )
    except subprocess.CalledProcessError as e:
        print(f"Failed to build: {e}")
        return False

    # Verify
    server_binary = build_dir / "bin" / "llama-server"
    if server_binary.exists():
        print(f"llama-server built successfully: {server_binary}")

        # Record version
        vm = LlamaVersionManager()
        info = vm.get_installed_version()
        if info:
            vm.version_file.write_text(
                json.dumps({
                    "version": info.version,
                    "installed_at": time.time(),
                }, indent=2)
            )

        return True
    else:
        print("Build completed but llama-server not found.")
        return False


def download_model() -> bool:
    """Download a 27B model."""
    print("Downloading 27B model...")
    print("Options:")
    print("  1. Qwen-2.5-27B (GGUF Q4_K_M) - ~14.5GB")
    print("  2. Llama-3.1-27B (GGUF Q4_K_M) - ~15GB")
    print("  3. Custom URL")
    print()

    choice = input("Select option (1-3): ").strip()

    model_dir = Path.home() / ".local/share/cupola/models"
    model_dir.mkdir(parents=True, exist_ok=True)

    if choice == "1":
        url = "https://huggingface.co/Qwen/Qwen2.5-27B-GGUF/resolve/main/qwen2.5-27b-q4_k_m.gguf"
        filename = "qwen2.5-27b-q4_k_m.gguf"
    elif choice == "2":
        url = "https://huggingface.co/meta-llama/Llama-3.1-27B-GGUF/resolve/main/llama-3.1-27b-q4_k_m.gguf"
        filename = "llama-3.1-27b-q4_k_m.gguf"
    elif choice == "3":
        url = input("Enter model URL: ").strip()
        filename = url.split("/")[-1]
    else:
        print("Invalid choice.")
        return False

    target = model_dir / filename
    print(f"Downloading to {target}...")

    try:
        subprocess.run(
            ["wget", "-c", "-O", str(target), url],
            check=True
        )
        print(f"Downloaded: {target}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Download failed: {e}")
        return False


def configure_profile(binary: Path, model: Path) -> bool:
    """Configure Cupola profile for the detected installation."""
    print("Configuring Cupola profile...")

    config = load_config()

    # Generate profile alias
    model_name = model.name.replace(".gguf", "").replace(".bin", "")
    alias = re.sub(r"[^a-z0-9_-]", "-", model_name.lower())[:20]

    profile = ProfileSpec(
        alias=alias,
        weights_path=model,
        backend_binary=binary,
    )

    config.profiles[alias] = profile
    config.default_profile = alias

    save_config(config)
    print(f"Profile '{alias}' configured in {config.config_dir / 'config.toml'}")
    return True


def test_connection(port: int = 8080, timeout: int = 5) -> bool:
    """Test connection to llama-server."""
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as response:
            if response.status == 200:
                print(f"✓ Connected to llama-server on port {port}")
                return True
    except Exception as e:
        print(f"✗ Failed to connect: {e}")

    return False


def show_status(info: InstallationInfo) -> None:
    """Display detected installation status."""
    print("══════════════════════════════════════════════════════════════")
    print("DETECTED INSTALLATION STATUS")
    print("══════════════════════════════════════════════════════════════")
    print()

    if info.gpu_detected:
        print(f"✓ GPU detected: {info.gpu_name}")
    else:
        print("✗ GPU: NOT DETECTED")

    if info.rocm_installed:
        print("✓ ROCm installed")
        if info.rocm_version:
            print(f"  Version: {info.rocm_version}")
    else:
        print("✗ ROCm: NOT INSTALLED")

    if info.llama_server_binary:
        print(f"✓ llama-server binary: {info.llama_server_binary}")
        if info.llama_server_version:
            print(f"  Version: {info.llama_server_version}")

        # Check if update is available
        vm = LlamaVersionManager()
        if vm.needs_update():
            print(f"  ⚠ Update available: {vm.get_latest_version()}")
    else:
        print("✗ llama-server binary: NOT FOUND")

    if info.systemd_service:
        print(f"✓ systemd service: {info.systemd_service}")
    else:
        print("✗ systemd service: NOT FOUND")

    if info.running_process:
        print(f"✓ Running process: PID {info.running_process['pid']}")
        print(f"  Command: {info.running_process['cmdline'][:80]}...")
    else:
        print("✗ Running process: NOT FOUND")

    if info.model_files:
        print(f"✓ Model files found: {len(info.model_files)}")
        for model in info.model_files[:5]:
            print(f"  - {model}")
    else:
        print("✗ Model files: NOT FOUND")

    if info.config_file and info.config_file.exists():
        print(f"✓ Cupola config: {info.config_file}")
    else:
        print("✗ Cupola config: NOT FOUND")

    print()


def run_setup_wizard() -> int:
    """Run the interactive setup wizard."""
    while True:
        clear_screen()
        print_banner()

        print("Main Menu:")
        print("  1. Detect existing installation")
        print("  2. Install ROCm drivers")
        print("  3. Install llama.cpp (with ROCm)")
        print("  4. Download 27B model")
        print("  5. Configure Cupola profile")
        print("  6. Test connection")
        print("  7. Check for llama.cpp updates")
        print("  8. Exit")
        print()

        choice = input("Select option (1-8): ").strip()

        if choice == "1":
            print("Detecting installation...")
            info = detect_installation()
            show_status(info)
            input("Press Enter to continue...")

        elif choice == "2":
            if install_rocm():
                print("ROCm installation successful!")
            else:
                print("ROCm installation failed.")
            input("Press Enter to continue...")

        elif choice == "3":
            info = detect_installation()
            if info.rocm_installed:
                print("ROCm detected, building with ROCm support...")
                if install_llama_cpp(rocm=True):
                    print("Installation successful!")
                else:
                    print("Installation failed.")
            else:
                print("ROCm not detected. Install ROCm first (option 2).")
                print("Or install llama.cpp without ROCm support?")
                confirm = input("Install without ROCm? (y/N): ").strip().lower()
                if confirm == "y":
                    if install_llama_cpp(rocm=False):
                        print("Installation successful!")
                    else:
                        print("Installation failed.")
                else:
                    print("Please install ROCm first.")
            input("Press Enter to continue...")

        elif choice == "4":
            if download_model():
                print("Model downloaded successfully!")
            else:
                print("Download failed.")
            input("Press Enter to continue...")

        elif choice == "5":
            info = detect_installation()
            if info.llama_server_binary and info.model_files:
                model = info.model_files[0]
                if configure_profile(info.llama_server_binary, model):
                    print("Profile configured successfully!")
                else:
                    print("Profile configuration failed.")
            else:
                print("Need llama-server binary and model file first.")
                print("Run options 3 and 4.")
            input("Press Enter to continue...")

        elif choice == "6":
            port = input("Port (default 8080): ").strip()
            if not port:
                port = "8080"
            if test_connection(int(port)):
                print("Connection test passed!")
            else:
                print("Connection test failed.")
            input("Press Enter to continue...")

        elif choice == "7":
            vm = LlamaVersionManager()
            installed = vm.get_installed_version()
            if installed:
                print(f"Installed version: {installed.version}")
                if vm.needs_update():
                    print(f"Update available: {vm.get_latest_version()}")
                    confirm = input("Update now? (y/N): ").strip().lower()
                    if confirm == "y":
                        if vm.update():
                            print("Update completed successfully!")
                        else:
                            print("Update failed.")
                else:
                    print("Already up to date!")
            else:
                print("No llama.cpp installation detected.")
            input("Press Enter to continue...")

        elif choice == "8":
            print("Goodbye!")
            return 0

        else:
            print("Invalid choice.")
            input("Press Enter to continue...")

    return 0


if __name__ == "__main__":
    sys.exit(run_setup_wizard())
