"""Editing the host configuration from the interface: add, update, delete.

Rules:
  * config.json (no passwords) and secrets.json (passwords only) are stored separately;
  * every field is strictly validated by Pydantic models (see HostInput) — nothing extra
    or invalid can be entered;
  * files are written atomically (to a temp file, then replaced) so the config is never corrupted;
  * changes can be applied only when no stack or host restart is running.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .config import (
    ALLOWED_SERVICE_TYPES,
    DEFAULT_SERVICE_TYPE,
    AppConfig,
    OSType,
    ServiceType,
    load_config,
)

# fixed column order for well-known groups
CANONICAL_GROUP_ORDER = {"All machines": 1, "Backup machines": 2, "Production machines": 3}

HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$")


# a string that looks like IPv4 (digits and dots) — validated strictly as an IP, not a domain
LOOKS_LIKE_IPV4 = re.compile(r"^[0-9.]+$")


def valid_hostname(value: str) -> bool:
    """Strict check: a valid IPv4/IPv6 OR a valid domain name.

    A string of only digits and dots (e.g. 10.0.0.2404444) is treated as an IP and must
    be a valid IP address — it cannot be a domain name.
    """
    value = value.strip()
    if not value:
        return False
    if LOOKS_LIKE_IPV4.match(value):
        try:
            ipaddress.ip_address(value)  # rejects 256.1.1.1, 10.0.0 and 10.0.0.2404444
            return True
        except ValueError:
            return False
    try:
        ipaddress.ip_address(value)  # IPv6
        return True
    except ValueError:
        return bool(HOSTNAME_RE.match(value))


class HostInput(BaseModel):
    """Data for a single host from the form. Forbids extra fields (extra=forbid)."""

    model_config = {"extra": "forbid"}

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    hostname: str = Field(min_length=1, max_length=253)
    os: OSType
    user: str = Field(min_length=1, max_length=256)
    group: str = Field(min_length=1, max_length=120, description="Name of the group (stage) to place the host in")
    port: int | None = Field(None, ge=1, le=65535)
    timeout_seconds: int = Field(300, ge=10, le=3600)

    # application
    app_service_name: str | None = Field(None, max_length=256)
    app_service_type: ServiceType | None = None
    app_process_name: str | None = Field(None, max_length=256)
    app_health_port: int | None = Field(None, ge=1, le=65535)
    app_start_timeout_seconds: int = Field(120, ge=5, le=3600)

    # user session (GUI/kiosk)
    requires_user_session: bool = False
    session_user: str | None = Field(None, max_length=256)
    login_timeout_seconds: int = Field(180, ge=10, le=3600)

    # Windows WinRM
    winrm_https: bool = False
    winrm_transport: Literal["ntlm", "kerberos", "basic", "credssp"] = "ntlm"
    winrm_verify_ssl: bool = True

    # monitoring
    zabbix_host: str | None = Field(None, max_length=253)

    # secrets (never go into config.json, written to secrets.json)
    password: str | None = Field(None, max_length=512)
    sudo_password: str | None = Field(None, max_length=512)
    ssh_key: str | None = Field(None, max_length=1024)
    ssh_key_passphrase: str | None = Field(None, max_length=512)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", v):
            raise ValueError("id: Latin letters, digits and the characters _ - . only")
        return v

    @field_validator("hostname", "zabbix_host")
    @classmethod
    def _check_hostname(cls, v: str | None) -> str | None:
        if v and not valid_hostname(v):
            raise ValueError(f"\"{v}\" does not look like an IP address or domain name")
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> HostInput:
        # auto-detect the application type (the field was removed from the form for simplicity):
        #   Linux + user session needed      -> graphical kiosk (desktop_autostart)
        #   Mac   + user session needed      -> launchagent (in the session)
        #   Windows + session / set by process/port -> scheduled task (windows_task)
        #   otherwise -> the default service for the OS
        if self.app_service_name and self.app_service_type is None:
            if self.os == "linux" and self.requires_user_session:
                self.app_service_type = "desktop_autostart"
            elif self.os == "mac" and self.requires_user_session:
                self.app_service_type = "launchagent"
            elif self.os == "windows" and (self.requires_user_session or self.app_process_name or self.app_health_port):
                self.app_service_type = "windows_task"
            else:
                self.app_service_type = DEFAULT_SERVICE_TYPE[self.os]
        if self.app_service_type and self.app_service_type not in ALLOWED_SERVICE_TYPES[self.os]:
            allowed = ", ".join(sorted(ALLOWED_SERVICE_TYPES[self.os]))
            raise ValueError(f"For OS {self.os} the application type can be: {allowed}")

        # user session — Windows/Mac only (and Linux desktop_autostart)
        if self.requires_user_session:
            if self.os == "linux" and self.app_service_type not in (None, "desktop_autostart"):
                raise ValueError("For Linux a user session is available only with type desktop_autostart")
            if not self.session_user:
                raise ValueError("Specify the autologon user (session_user) since a user session is required")

        # desktop_autostart requires a process and a session
        if self.app_service_type == "desktop_autostart":
            if not self.requires_user_session:
                raise ValueError("desktop_autostart requires \"user login needed\"")
            if not self.app_process_name and not self.app_health_port:
                raise ValueError("For desktop_autostart specify a process name or an application port")

        # launchagent — only with a session
        if self.app_service_type == "launchagent" and not (self.requires_user_session and self.session_user):
            raise ValueError("launchagent requires \"user login needed\" and session_user")

        # password OR key for access
        if self.os == "windows" and not self.password:
            raise ValueError("Windows requires a password (WinRM)")
        if self.os in ("linux", "mac") and not self.password and not self.ssh_key:
            raise ValueError("For Linux/macOS specify an SSH key or a password")

        return self

    def to_host_json(self) -> dict:
        """Host fields for config.json (without secrets)."""
        data = self.model_dump(exclude_none=True)
        for secret in ("password", "sudo_password", "ssh_key", "ssh_key_passphrase", "group"):
            data.pop(secret, None)
        creds: dict = {}
        if self.ssh_key:
            creds["ssh_key"] = self.ssh_key
        if creds:
            data["credentials"] = creds
        return data

    def secret_json(self) -> dict:
        """Host secrets for secrets.json."""
        out = {}
        if self.password:
            out["password"] = self.password
        if self.sudo_password:
            out["sudo_password"] = self.sudo_password
        if self.ssh_key_passphrase:
            out["ssh_key_passphrase"] = self.ssh_key_passphrase
        return out


class GroupInput(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=120)
    order: int = Field(ge=1, le=999)
    mode: Literal["sequential", "parallel"] = "parallel"
    on_failure: Literal["halt", "continue"] = "continue"


class ConfigStore:
    """Reading and writing config.json + secrets.json with validation."""

    def __init__(self, config_path: str | os.PathLike):
        self.config_path = Path(config_path)

    @property
    def secrets_path(self) -> Path:
        cfg = self._raw()
        name = cfg.get("settings", {}).get("secrets_file", "secrets.json")
        p = Path(name)
        return p if p.is_absolute() else self.config_path.parent / p

    def _raw(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def _read_secrets(self) -> dict:
        try:
            return json.loads(self.secrets_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _atomic_write(path: Path, data: dict, secret: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            if secret and os.name == "posix":
                os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def list_groups(self) -> list[str]:
        return [g["name"] for g in self._raw().get("groups", [])]

    def validate_and_apply(self, action: str, payload: dict) -> AppConfig:
        """Apply a change (add/update/delete) and save. Returns a new AppConfig."""
        raw = self._raw()
        secrets = self._read_secrets()
        groups = raw.setdefault("groups", [])
        existing_ids = {h["id"] for g in groups for h in g.get("hosts", [])}

        if action == "delete":
            host_id = payload.get("id")
            if host_id not in existing_ids:
                raise ValueError(f"Host {host_id} not found")
            for g in groups:
                g["hosts"] = [h for h in g.get("hosts", []) if h["id"] != host_id]
            raw["groups"] = [g for g in groups if g.get("hosts")]  # drop groups that became empty
            secrets.pop(host_id, None)

        elif action in ("add", "update"):
            try:
                host = HostInput.model_validate(payload)
            except ValidationError as exc:
                raise ValueError(self._format_errors(exc)) from exc

            if action == "add" and host.id in existing_ids:
                raise ValueError(f"A host with id \"{host.id}\" already exists")
            if action == "update" and host.id not in existing_ids:
                raise ValueError(f"Host \"{host.id}\" not found for update")

            # remove the old entry (in case the group changed)
            for g in groups:
                g["hosts"] = [h for h in g.get("hosts", []) if h["id"] != host.id]

            # find or create the group
            target = next((g for g in groups if g["name"] == host.group), None)
            if target is None:
                order = CANONICAL_GROUP_ORDER.get(host.group)
                if order is None:
                    order = max((g.get("order", 0) for g in groups), default=0) + 1
                target = {"name": host.group, "order": order, "mode": "parallel",
                          "on_failure": "continue", "hosts": []}
                groups.append(target)
            target.setdefault("hosts", []).append(host.to_host_json())
            raw["groups"] = [g for g in groups if g.get("hosts")]

            sec = host.secret_json()
            if sec:
                secrets[host.id] = sec
            else:
                secrets.pop(host.id, None)
        else:
            raise ValueError(f"Unknown action: {action}")

        # final integrity check: let AppConfig confirm everything assembles
        try:
            AppConfig.model_validate(raw)
        except ValidationError as exc:
            raise ValueError("Resulting configuration is invalid: " + self._format_errors(exc)) from exc

        # save both files
        self._atomic_write(self.config_path, raw)
        self._atomic_write(self.secrets_path, secrets, secret=True)
        return load_config(self.config_path)

    @staticmethod
    def _format_errors(exc: ValidationError) -> str:
        parts = []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e["loc"]) or "field"
            parts.append(f"{loc}: {e['msg']}")
        return "; ".join(parts)

    def host_for_edit(self, host_id: str) -> dict:
        """Host data for the edit form (without passwords, but noting whether they are set)."""
        for g in self._raw().get("groups", []):
            for h in g.get("hosts", []):
                if h["id"] == host_id:
                    out = dict(h)
                    out["group"] = g["name"]
                    creds = out.pop("credentials", {}) or {}
                    out["ssh_key"] = creds.get("ssh_key")
                    sec = self._read_secrets().get(host_id, {})
                    out["has_password"] = bool(sec.get("password"))
                    out["has_sudo_password"] = bool(sec.get("sudo_password"))
                    return out
        raise ValueError(f"Host {host_id} not found")
