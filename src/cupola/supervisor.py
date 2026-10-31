"""Crucible — process supervisor with state machine."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from cupola.config import CupolaConfig, ProfileSpec
from cupola.logging import OperationalLogger
from cupola.telemetry import SentinelSampler


@dataclass
class InstanceRecord:
    """Record of a launched backend instance."""
    instance_uuid: str = field(default_factory=lambda: str(uuid.uuid4()))
    pid: Optional[int] = None
    start_time: Optional[int] = None  # from /proc/[pid]/stat
    port: int = 8080
    profile: str = ""
    state: str = "STOPPED"  # STOPPED, VALIDATING, STARTING, READY, STOPPING, FAILED, DEGRADED
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_error: Optional[str] = None
    weights_path: Optional[str] = None
    host: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "instance_uuid": self.instance_uuid,
            "pid": self.pid,
            "start_time": self.start_time,
            "port": self.port,
            "profile": self.profile,
            "state": self.state,
            "created_at": self.created_at,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InstanceRecord":
        return cls(**data)


class Supervisor:
    """Manages llama-server lifecycle with state machine."""

    def __init__(
        self,
        state_dir: Path,
        logger: OperationalLogger,
        runtime_dir: Optional[Path] = None,
        config: Optional[CupolaConfig] = None,
    ):
        self.state_dir = state_dir
        self.logger = logger
        if runtime_dir is None:
            runtime_dir = state_dir / "runtime"
        self.runtime_dir = runtime_dir
        self.config = config
        self._sampler: Optional[SentinelSampler] = None

    def _read_proc_stat(self, pid: int) -> Optional[int]:
        """Read start_time from /proc/[pid]/stat. Returns None if process gone.

        Field 2 (comm) can contain spaces and parentheses, so we find the last ')'
        and split from there to get reliable field positions.
        """
        try:
            with open(f"/proc/{pid}/stat") as f:
                content = f.read()
            # Find end of comm field (last ')' in the file)
            idx = content.rfind(")")
            if idx < 0:
                return None
            # Fields after comm: state(3) ... start_time(22) = 20th field after ')'
            # After ')' comes: ' state ppid pgrp session tty_nr tpgid flags ...'
            # start_time is field 22 overall, which is index 19 in the array after
            # splitting on spaces starting after the ')'.
            after_comm = content[idx + 1:].strip()
            fields = after_comm.split()
            # after_comm fields: state(0), ppid(1), pgrp(2), session(3), tty_nr(4),
            # tpgid(5), flags(6), minflt(7), cminflt(8), majflt(9), cmajflt(10),
            # utime(11), stime(12), cutime(13), cstime(14), priority(15), nice(16),
            # num_threads(17), itrealvalue(18), starttime(19)
            return int(fields[20])
        except (FileNotFoundError, OSError, IndexError, ValueError):
            return None

    def _verify_process(self, pid: int, expected_start_time: int,
                        expected_uuid: Optional[str] = None) -> bool:
        """Verify a PID is still our process (not PID-wrapped).

        If expected_uuid is provided, also verify the process environment
        contains the matching instance UUID via /proc/[pid]/environ.
        """
        actual_start = self._read_proc_stat(pid)
        if actual_start is None or actual_start != expected_start_time:
            return False

        # UUID verification: check process environment for our instance marker
        if expected_uuid is not None:
            try:
                with open(f"/proc/{pid}/environ", "rb") as f:
                    environ = f.read()
                # environ is null-separated key=value pairs
                marker = f"__CUPOLA_INSTANCE={expected_uuid}".encode()
                if marker not in environ:
                    return False
            except (FileNotFoundError, OSError):
                return False

        return True

    def _send_signal(self, pid: int, sig: int) -> bool:
        """Send signal to process. Returns False if process gone.

        On PermissionError, the PID exists but belongs to another process
        (PID wrap or reuse) — not ours.
        """
        try:
            os.kill(pid, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # PID exists but is not our process
            self.logger.warn("signal_permission_denied", pid=pid, signal=sig)
            return False

    def _poll_exit(self, pid: int, timeout: float) -> bool:
        """Wait for process to exit. Returns True if exited, False if timed out."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._process_exists(pid):
                return True
            time.sleep(0.1)
        return not self._process_exists(pid)

    def _process_exists(self, pid: int) -> bool:
        """Check if process exists."""
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _start_sentinel(self, instance: InstanceRecord) -> None:
        """Start Sentinel sampler for a verified instance."""
        if self._sampler is not None:
            return

        if self.config is None:
            return

        self.logger.info("sentinel_start", instance_uuid=instance.instance_uuid)
        self._sampler = SentinelSampler(
            instance_uuid=instance.instance_uuid,
            profile=instance.profile,
            pid=instance.pid,
            start_time=instance.start_time or 0,
            state_dir=self.state_dir,
            sample_hz=self.config.sentinel.sample_hz,
            warn_hotspot_c=self.config.sentinel.warn_hotspot_c,
            critical_hotspot_c=self.config.sentinel.critical_hotspot_c,
            critical_hold_s=self.config.sentinel.critical_hold_s,
            auto_stop_on_critical=self.config.sentinel.auto_stop_on_critical,
            assume_power_w=self.config.sentinel.assume_power_w,
            wall_energy_path=self.config.sentinel.wall_energy_path,
            accounting=self.config.accounting.__dict__,
            stop_callback=self._sentinel_stop_callback,
        )
        self._sampler.start()

    def _stop_sentinel(self) -> Optional[dict]:
        """Stop Sentinel sampler and return session summary."""
        if self._sampler is None:
            return None

        self.logger.info("sentinel_stop")
        summary = self._sampler.stop()
        self._sampler = None
        return summary.to_dict()

    def _sentinel_stop_callback(self, instance_uuid: str) -> None:
        """Callback from Sentinel when critical thermal threshold reached."""
        self.logger.warn("sentinel_critical_stop", instance_uuid=instance_uuid)
        # Trigger stop through normal stop path
        try:
            self.stop()
        except Exception as e:
            self.logger.error("sentinel_stop_failed", error=str(e))

    def get_instance(self) -> Optional[InstanceRecord]:
        """Load current instance record."""
        instance_path = self.state_dir / "instance.json"
        if not instance_path.exists():
            return None
        try:
            with open(instance_path) as f:
                data = json.load(f)
            return InstanceRecord.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            return None

    def save_instance(self, instance: InstanceRecord) -> None:
        """Save instance record."""
        import tempfile

        self.state_dir.mkdir(parents=True, exist_ok=True)
        instance_path = self.state_dir / "instance.json"
        fd, tmp_path = tempfile.mkstemp(dir=self.state_dir, prefix=".instance.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(instance.to_dict(), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_path, instance_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def launch(
        self,
        profile: ProfileSpec,
        timeout: float = 30.0,
        foreground: bool = False,
    ) -> InstanceRecord:
        """Launch llama-server for a profile. State: STOPPED→VALIDATING→STARTING→READY.

        If foreground is True, the subprocess stdout/stderr are attached to the
        controlling terminal instead of a log file.
        """
        self.logger.info("launch_start", profile=profile.alias, ctx_size=profile.ctx_size,
                         port=profile.port)

        # Check for existing DEGRADED instance — blocks new launches
        existing = self.get_instance()
        if existing and existing.state == "DEGRADED":
            instance = InstanceRecord(profile=profile.alias)
            instance.state = "FAILED"
            instance.last_error = (
                f"Previous instance is DEGRADED (PID {existing.pid}). "
                "Resolve ownership before launching."
            )
            self.logger.error("launch_blocked_degraded", profile=profile.alias,
                              existing_pid=existing.pid)
            self.save_instance(instance)
            return instance

        # Validate first
        from cupola.config import validate_profile

        errors = validate_profile(profile)
        if errors:
            self.logger.warn("launch_failed_validation", profile=profile.alias,
                             errors=errors)
            instance = InstanceRecord(profile=profile.alias)
            instance.state = "FAILED"
            instance.last_error = "; ".join(errors)
            self.save_instance(instance)
            return instance

        # Build command
        self.logger.info("launch_building_cmd", profile=profile.alias)
        cmd = [
            str(profile.backend_binary),
            "-m", str(profile.weights_path),
            "--host", profile.host,
            "--port", str(profile.port),
            "--n-gpu-layers", str(profile.n_gpu_layers),
            "--ctx-size", str(profile.ctx_size),
            "--cache-type-k", profile.cache_type_k,
            "--cache-type-v", profile.cache_type_v,
            "--batch-size", str(profile.batch_size),
            "--ubatch-size", str(profile.ubatch_size),
            "--threads", str(profile.threads),
            "--parallel", str(profile.n_parallel),
        ]
        if profile.flash_attn:
            cmd.append("--flash-attn")
        if profile.chat_template and profile.chat_template.exists():
            cmd.extend(["--chat-template", str(profile.chat_template)])

        # Check port conflict — probe only, do not bind (TOCTOU avoidance)
        import socket

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.connect((profile.host, profile.port))
            # If connect succeeds, port is in use
            instance = InstanceRecord(profile=profile.alias)
            instance.state = "FAILED"
            instance.last_error = f"Port {profile.port} is already in use"
            self.logger.error("launch_port_conflict", profile=profile.alias,
                              port=profile.port)
            self.save_instance(instance)
            return instance
        except ConnectionRefusedError:
            # Port is free — proceed
            pass
        except OSError:
            # Network unreachable or other OS error; proceed to let backend report
            pass

        # Spawn — stdout/stderr go to log file, not PIPE (avoids blocking)
        instance = InstanceRecord(
            profile=profile.alias,
            port=profile.port,
            state="STARTING",
        )

        # Add instance UUID marker to environment for verification
        env = os.environ.copy()
        env["__CUPOLA_INSTANCE"] = instance.instance_uuid

        try:
            if foreground:
                proc = subprocess.Popen(cmd, env=env)
            else:
                backend_log = self.logger.log_dir / f"backend-{profile.alias}.log"
                with open(backend_log, "w") as log_file:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        env=env,
                    )
            instance.pid = proc.pid
            instance.start_time = self._read_proc_stat(proc.pid)
            self.logger.info("launch_spawned", profile=profile.alias, pid=proc.pid)
            self.save_instance(instance)
        except OSError as e:
            instance.state = "FAILED"
            instance.last_error = f"Failed to spawn llama-server: {e}"
            self.save_instance(instance)
            return instance

        # Poll /health until ready
        import urllib.request

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                req = urllib.request.Request(f"http://{profile.host}:{profile.port}/health")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status == 200:
                        instance.state = "READY"
                        self.logger.info("launch_ready", profile=profile.alias,
                                          pid=proc.pid, port=profile.port)
                        self.save_instance(instance)
                        # Start Sentinel telemetry for verified instance
                        self._start_sentinel(instance)
                        return instance
            except Exception:
                pass

            # Check if process died
            if not self._verify_process(instance.pid, instance.start_time or 0):
                instance.state = "FAILED"
                instance.last_error = "Backend process exited before becoming ready"
                self.save_instance(instance)
                return instance

            time.sleep(0.5)

        instance.state = "FAILED"
        instance.last_error = f"Backend did not become ready within {timeout}s"
        self.logger.error("launch_timeout", profile=profile.alias, timeout=timeout)
        self.save_instance(instance)
        return instance

    def stop(self, timeout: float = 10.0, kill_timeout: float = 5.0) -> InstanceRecord:
        """Stop the backend. State: READY/STARTING→STOPPING→STOPPED/DEGRADED."""
        instance = self.get_instance()
        if not instance or instance.state == "STOPPED":
            # Still stop sentinel if running
            self._stop_sentinel()
            return instance or InstanceRecord(state="STOPPED")

        # Stop Sentinel before stopping backend
        self._stop_sentinel()

        if not instance.pid:
            instance.state = "STOPPED"
            self.save_instance(instance)
            return instance

        instance.state = "STOPPING"
        self.logger.info("stop_starting", profile=instance.profile, pid=instance.pid)
        self.save_instance(instance)

        # Verify process is still ours (PID + start_time + instance_uuid)
        if not self._verify_process(instance.pid, instance.start_time or 0,
                                    instance.instance_uuid):
            # Process already gone or PID reused
            instance.state = "STOPPED"
            self.save_instance(instance)
            return instance

        # SIGTERM
        self.logger.info("stop_sigterm", profile=instance.profile, pid=instance.pid)
        self._send_signal(instance.pid, signal.SIGTERM)
        if self._poll_exit(instance.pid, timeout):
            instance.state = "STOPPED"
            self.logger.info("stop_completed", profile=instance.profile)
            self.save_instance(instance)
            return instance

        # SIGKILL
        self.logger.warn("stop_sigkill", profile=instance.profile, pid=instance.pid)
        self._send_signal(instance.pid, signal.SIGKILL)
        if self._poll_exit(instance.pid, kill_timeout):
            instance.state = "STOPPED"
            self.logger.info("stop_completed", profile=instance.profile)
            self.save_instance(instance)
            return instance

        # Process still alive — degraded
        instance.state = "DEGRADED"
        instance.last_error = "Backend process did not exit after SIGKILL"
        self.logger.error("stop_degraded", profile=instance.profile, pid=instance.pid)
        self.save_instance(instance)
        return instance

    def status(self) -> InstanceRecord:
        """Get current instance status."""
        instance = self.get_instance()
        if not instance:
            return InstanceRecord(state="STOPPED")

        # If marked READY/STARTING but process gone, transition to STOPPED
        if instance.state in ("READY", "STARTING") and instance.pid:
            if instance.start_time is None:
                # No start_time recorded — cannot verify, treat as dead
                instance.state = "STOPPED"
                self.save_instance(instance)
            elif not self._verify_process(instance.pid, instance.start_time,
                                         instance.instance_uuid):
                instance.state = "STOPPED"
                self.save_instance(instance)

        return instance