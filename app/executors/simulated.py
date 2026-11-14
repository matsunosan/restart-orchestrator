"""Host simulation for dry_run mode: lets you test the UI and logic without real reboots."""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass

from .base import ExecutorError, HostExecutor


@dataclass
class _SimHost:
    boot_id: int = 1
    down_until: float = 0.0
    app_start_at: float = 0.0
    app_running: bool = True
    app_broken: bool = False


_SIM: dict[str, _SimHost] = {}
_LOCK = threading.Lock()


def _state(host_id: str) -> _SimHost:
    with _LOCK:
        return _SIM.setdefault(host_id, _SimHost())


def is_sim_host_up(host_id: str) -> bool:
    return time.time() >= _state(host_id).down_until


class SimulatedExecutor(HostExecutor):
    def _up(self) -> _SimHost:
        time.sleep(random.uniform(0.1, 0.4))  # simulated network latency
        s = _state(self.host.id)
        now = time.time()
        if now < s.down_until:
            raise ExecutorError("Host unreachable (simulation)")
        if not s.app_running and not s.app_broken and now >= s.app_start_at:
            s.app_running = True  # service "autostart" after boot
        return s

    def precheck(self) -> None:
        self._up()

    def get_boot_marker(self) -> str:
        return f"sim-boot-{self._up().boot_id}"

    def reboot(self) -> None:
        s = self._up()
        now = time.time()
        rate = self.settings.dry_run_failure_rate
        roll = random.random()
        s.boot_id += 1
        s.app_running = False
        s.app_broken = False
        if roll < rate:
            # the host "hung" during boot longer than the timeout
            s.down_until = now + self.host.timeout_seconds + 30
        else:
            s.down_until = now + random.uniform(6, 18)
            s.app_broken = roll < rate * 2
        s.app_start_at = s.down_until + random.uniform(2, 6)

    def ensure_autostart(self) -> str:
        self._up()
        return f"{self.host.app_service_type}: {self.service} autostart enabled (simulation)"

    def is_app_running(self) -> bool:
        return self._up().app_running

    def start_app(self) -> None:
        s = self._up()
        if s.app_broken:
            raise ExecutorError("Process exited with code 1 right after start (simulation)")
        s.app_running = True

    def autostart_status(self) -> str:
        self._up()
        return "enabled (simulation)"

    def session_user(self) -> str | None:
        self._up()
        return (self.host.session_user or "operator") if self.host.requires_user_session else None

    def can_start_manually(self) -> bool:
        return self.host.app_service_type != "desktop_autostart"

    def port_open(self, connect_timeout: float = 3.0) -> bool:
        return is_sim_host_up(self.host.id)

    def app_diagnostics(self) -> str:
        return f"{self.service}: main process exited, code=exited, status=1/FAILURE (simulation)"
