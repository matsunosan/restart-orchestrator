"""Common interface for command executors on remote hosts.

All methods are synchronous (blocking): the orchestrator calls them via asyncio.to_thread,
so the web server is not blocked.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Host, Settings


class ExecutorError(Exception):
    """Connection or command-execution error on a host."""


class ConnectionLost(ExecutorError):
    """The connection dropped during command execution (e.g. the host started rebooting)."""


class HostExecutor(ABC):
    def __init__(self, host: Host, settings: Settings, vault: object | None = None):
        self.host = host
        self.settings = settings
        self.vault = vault  # app.vault.Vault or None (no-encryption mode)

    def _reveal(self, value: str | None) -> str | None:
        """Plaintext password: via the vault (decryption), or as-is if there is no vault."""
        if self.vault is not None:
            return self.vault.reveal(value)
        return value

    def get_password(self) -> str | None:
        return self._reveal(self.host.credentials.get_password())

    def get_sudo_password(self) -> str | None:
        return self._reveal(self.host.credentials.get_sudo_password())

    @property
    def service(self) -> str | None:
        return self.host.app_service_name

    @abstractmethod
    def precheck(self) -> None:
        """Check access, privileges and the service presence. Called BEFORE the reboot."""

    @abstractmethod
    def get_boot_marker(self) -> str:
        """A unique identifier of the current OS boot (boot_id / boot time)."""

    @abstractmethod
    def reboot(self) -> None:
        """Send the reboot command. Must return immediately, without waiting for shutdown."""

    @abstractmethod
    def ensure_autostart(self) -> str:
        """Ensure the application autostarts on OS boot. Returns a description."""

    @abstractmethod
    def is_app_running(self) -> bool:
        """Whether the application/service is running now."""

    @abstractmethod
    def start_app(self) -> None:
        """Start the application/service manually (self-healing)."""

    def app_diagnostics(self) -> str:
        """Service log/state for incident investigation."""
        return ""

    def autostart_status(self) -> str:
        """Read-only: how the application autostart is currently configured (for a check without reboot)."""
        return "not checked"

    def session_user(self) -> str | None:
        """Who is logged in at the console; None — the login screen. Needed for requires_user_session."""
        return None

    def can_start_manually(self) -> bool:
        """Whether the application can be started remotely. For a kiosk GUI — no (a desktop is required)."""
        return True

    def port_open(self, connect_timeout: float = 3.0) -> bool:
        """Whether the management port (SSH/WinRM) is open. A fast TCP check."""
        return self._tcp_open(self.host.hostname, self.host.port, connect_timeout)

    @staticmethod
    def _tcp_open(host: str, port: int, connect_timeout: float = 3.0) -> bool:
        import socket
        try:
            with socket.create_connection((host, port), timeout=connect_timeout):
                return True
        except OSError:
            return False

    def app_health_port_open(self, connect_timeout: float = 3.0) -> bool:
        """Whether the application port is open (a sign the app is running). Without WinRM/SSH."""
        port = self.host.app_health_port
        if not port:
            return False
        host = self.host.app_health_host or self.host.hostname
        return self._tcp_open(host, port, connect_timeout)

    def has_port_health(self) -> bool:
        return self.host.app_health_port is not None

    def describe(self) -> list[tuple[str, str]]:
        """Read-only host readiness info: [(level ok|info|warn|error, text)].

        Changes nothing on the host. Used by tools/check_hosts.py.
        """
        return []
