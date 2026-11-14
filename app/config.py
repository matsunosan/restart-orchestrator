"""Configuration models and loading of config.json."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, model_validator

OSType = Literal["linux", "windows", "mac"]
ServiceType = Literal["systemd", "launchd", "launchagent", "desktop_autostart", "windows_service", "windows_task"]

DEFAULT_SERVICE_TYPE = {"linux": "systemd", "mac": "launchd", "windows": "windows_service"}
ALLOWED_SERVICE_TYPES = {
    "linux": {"systemd", "desktop_autostart"},
    "mac": {"launchd", "launchagent"},
    "windows": {"windows_service", "windows_task"},
}


class Credentials(BaseModel):
    """Credentials. Passwords are best passed via environment variables (*_env)."""

    ssh_key: str | None = None
    ssh_key_passphrase_env: str | None = None
    password: str | None = None
    password_env: str | None = None
    # sudo with a password (Linux/macOS). Not set -> sudo -n (NOPASSWD) is used.
    # If the sudo password matches the login password, point to the same variable as password_env.
    sudo_password: str | None = None
    sudo_password_env: str | None = None
    # FileVault authrestart (macOS): a user allowed to unlock the disk and their password.
    # Defaults to host.user and the login password.
    filevault_user: str | None = None
    filevault_password_env: str | None = None

    def get_password(self) -> str | None:
        if self.password_env:
            return os.environ.get(self.password_env)
        return self.password

    def get_sudo_password(self) -> str | None:
        if self.sudo_password_env:
            value = os.environ.get(self.sudo_password_env)
            if not value:
                raise ValueError(f"Environment variable {self.sudo_password_env} (sudo password) is not set")
            return value
        return self.sudo_password

    def get_filevault_password(self) -> str | None:
        if self.filevault_password_env:
            return os.environ.get(self.filevault_password_env)
        return self.get_password()

    def get_key_passphrase(self) -> str | None:
        if self.ssh_key_passphrase_env:
            return os.environ.get(self.ssh_key_passphrase_env)
        return None


class Host(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    name: str
    hostname: str
    os: OSType
    user: str
    credentials: Credentials = Field(default_factory=Credentials)
    port: int | None = None
    timeout_seconds: int = Field(300, ge=10, description="How long to wait for the OS to boot after the reboot command")
    app_service_name: str | None = None
    app_service_type: ServiceType | None = None
    app_health_port: int | None = Field(
        None, description="Application port for a liveness check (Zabbix-style). If set, application readiness "
                          "is determined by this port opening, without WinRM. E.g. a Remote Control port like 30020")
    app_health_host: str | None = Field(
        None, description="Host for the application port check (defaults to hostname). Usually not needed")
    app_process_name: str | None = Field(
        None, description="Windows: process name (e.g. app.exe) to verify the application is running. "
                          "Needed when a task/batch starts the exe and exits — then the task state is not a reliable signal")
    app_start_timeout_seconds: int = Field(120, ge=5, description="How long to wait for the application autostart")
    requires_user_session: bool = Field(
        False, description="The application needs a user login (GUI). Windows/mac only, requires configured autologon")
    session_user: str | None = Field(None, description="Autologon user (required for launchagent)")
    login_timeout_seconds: int = Field(180, ge=10, description="How long to wait for autologon after the OS boots")
    zabbix_host: str | None = None
    winrm_transport: Literal["ntlm", "kerberos", "basic", "credssp"] = "ntlm"
    winrm_https: bool = False
    winrm_verify_ssl: bool = True
    exclude: bool = False  # explicitly exclude the host from restarts (e.g. the orchestrator machine itself)
    bitlocker_suspend: bool = False  # Windows: suspend BitLocker for one reboot (if a PIN is required at boot)
    filevault_authrestart: bool = False  # macOS: reboot via fdesetup authrestart (FileVault unlocks itself)

    @model_validator(mode="after")
    def _apply_defaults(self) -> Host:
        if self.port is None:
            if self.os == "windows":
                self.port = 5986 if self.winrm_https else 5985
            else:
                self.port = 22
        if self.app_service_name and self.app_service_type is None:
            self.app_service_type = DEFAULT_SERVICE_TYPE[self.os]
        if self.app_service_type and self.app_service_type not in ALLOWED_SERVICE_TYPES[self.os]:
            raise ValueError(
                f"Host {self.id}: service type {self.app_service_type} is not valid for OS {self.os}"
            )
        if self.app_service_type == "desktop_autostart":
            if not self.requires_user_session:
                raise ValueError(f"Host {self.id}: desktop_autostart requires requires_user_session=true")
            if not self.app_process_name:
                raise ValueError(f"Host {self.id}: desktop_autostart requires app_process_name (e.g. firefox)")
        if self.bitlocker_suspend and self.os != "windows":
            raise ValueError(f"Host {self.id}: bitlocker_suspend applies to Windows only")
        if self.filevault_authrestart and self.os != "mac":
            raise ValueError(f"Host {self.id}: filevault_authrestart applies to macOS only")
        return self


class Group(BaseModel):
    name: str
    order: int
    mode: Literal["sequential", "parallel"] = "sequential"
    on_failure: Literal["halt", "continue"] = "halt"
    hosts: list[Host] = Field(min_length=1)


class ZabbixSettings(BaseModel):
    enabled: bool = False
    url: str = "https://zabbix.example.com/api_jsonrpc.php"
    api_token_env: str = "ZABBIX_API_TOKEN"
    poll_interval_seconds: int = Field(30, ge=5)
    verify_ssl: bool = True


class Settings(BaseModel):
    dry_run: bool = True
    dry_run_failure_rate: float = Field(0.0, ge=0.0, le=0.5)
    poll_interval_seconds: int = Field(5, ge=1)
    app_poll_interval_seconds: int = Field(
        2, ge=1, description="How often to check application readiness and login (more often = faster reaction)")
    ping_interval_seconds: int = Field(10, ge=2)
    command_timeout_seconds: int = Field(60, ge=5)
    app_start_retries: int = Field(3, ge=0)
    app_retry_delay_seconds: int = Field(10, ge=1)
    max_parallel_hosts: int = Field(20, ge=1)
    ssh_known_hosts_file: str | None = None
    ssh_strict_host_key_checking: bool = True
    alert_webhook_url: str | None = None
    log_file: str = "logs/orchestrator.log"
    secrets_file: str | None = "secrets.json"
    master_key_clear_after_seconds: int = Field(
        7200, ge=0, description="After how many seconds of inactivity to wipe the master key from memory (0 = never)")
    self_detection: bool = True
    exclude_host_ids: list[str] = Field(default_factory=list)
    zabbix: ZabbixSettings = Field(default_factory=ZabbixSettings)


class AppConfig(BaseModel):
    settings: Settings = Field(default_factory=Settings)
    groups: list[Group] = Field(min_length=1)
    _warnings: list[str] = PrivateAttr(default_factory=list)

    @property
    def load_warnings(self) -> list[str]:
        return self._warnings

    @model_validator(mode="after")
    def _unique_ids(self) -> AppConfig:
        seen: set[str] = set()
        for host in self.all_hosts():
            if host.id in seen:
                raise ValueError(f"Duplicate host id: {host.id}")
            seen.add(host.id)
        return self

    def sorted_groups(self) -> list[Group]:
        return sorted(self.groups, key=lambda g: g.order)

    def all_hosts(self) -> list[Host]:
        return [h for g in self.sorted_groups() for h in g.hosts]

    def get_host(self, host_id: str) -> Host:
        for host in self.all_hosts():
            if host.id == host_id:
                return host
        raise KeyError(host_id)

    @staticmethod
    def _encrypted_prefix() -> str:
        return "enc:v1:"

    def public_view(self) -> list[dict]:
        """UI structure without credentials."""
        return [
            {
                "name": g.name,
                "order": g.order,
                "mode": g.mode,
                "on_failure": g.on_failure,
                "hosts": [
                    {
                        "id": h.id,
                        "name": h.name,
                        "hostname": h.hostname,
                        "os": h.os,
                        "app_service_name": h.app_service_name,
                        "app_service_type": h.app_service_type,
                        "timeout_seconds": h.timeout_seconds,
                        "requires_user_session": h.requires_user_session,
                        "session_user": h.session_user,
                        "needs_master": _is_encrypted_secret(h.credentials.password),
                    }
                    for h in g.hosts
                ],
            }
            for g in self.sorted_groups()
        ]


SECRET_KEYS = {"password", "sudo_password", "ssh_key_passphrase"}


def apply_secrets(config: AppConfig, path: Path) -> list[str]:
    """Apply passwords from secrets.json: {"<host id>": {"password": ..., "sudo_password": ...}}."""
    warnings: list[str] = []
    if not path.exists():
        return warnings
    if os.name == "posix" and path.stat().st_mode & 0o077:
        warnings.append(f"{path}: readable by other system users, run chmod 600 {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    known = {h.id for h in config.all_hosts()}
    for host_id in data:
        if host_id.startswith("_"):
            continue  # service keys (e.g. _verifier for encryption)
        if host_id not in known:
            warnings.append(f"{path}: host \"{host_id}\" is not in the configuration")
    for host in config.all_hosts():
        entry = data.get(host.id) or {}
        entry = {k: v for k, v in entry.items() if not k.startswith("_")}
        unknown = set(entry) - SECRET_KEYS
        if unknown:
            warnings.append(f"{path}: host \"{host.id}\" has unknown keys {sorted(unknown)}")
        for key in SECRET_KEYS & set(entry):
            setattr(host.credentials, key, entry[key])
    return warnings


def _is_encrypted_secret(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith("enc:v1:")


def load_config(path: str | os.PathLike) -> AppConfig:
    path = Path(path)
    config = AppConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if config.settings.secrets_file:
        secrets_path = Path(config.settings.secrets_file)
        if not secrets_path.is_absolute():
            secrets_path = path.parent / secrets_path
        config._warnings.extend(apply_secrets(config, secrets_path))
    unknown = set(config.settings.exclude_host_ids) - {h.id for h in config.all_hosts()}
    if unknown:
        config._warnings.append(f"exclude_host_ids: unknown ids {sorted(unknown)}")
    return config
