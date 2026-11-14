"""Executor factory by OS type."""
from __future__ import annotations

from ..config import Host, Settings
from .base import ExecutorError, HostExecutor

__all__ = ["ExecutorError", "HostExecutor", "get_executor"]


def get_executor(host: Host, settings: Settings, vault: object | None = None) -> HostExecutor:
    if settings.dry_run:
        from .simulated import SimulatedExecutor
        return SimulatedExecutor(host, settings, vault)
    if host.os == "linux":
        from .ssh import LinuxExecutor
        return LinuxExecutor(host, settings, vault)
    if host.os == "mac":
        from .ssh import MacExecutor
        return MacExecutor(host, settings, vault)
    if host.os == "windows":
        from .winrm_exec import WindowsExecutor  # lazy import: pywinrm is only needed for Windows
        return WindowsExecutor(host, settings, vault)
    raise ValueError(f"Unknown OS: {host.os}")
