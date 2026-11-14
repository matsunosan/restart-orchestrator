"""Configuration changes from the web interface: add, edit, delete hosts.

Passwords are written only to secrets.json, everything else to config.json. Writes are atomic
(via a temp file), with mandatory Pydantic validation before saving.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import SECRET_KEYS, AppConfig, Host, load_config

SECRET_FIELDS = sorted(SECRET_KEYS)


class ConfigError(Exception):
    """A configuration-change error (validation, write, duplicate id, etc.)."""


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)  # atomic replace
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _secrets_path(config_path: Path, config: AppConfig) -> Path:
    name = config.settings.secrets_file or "secrets.json"
    p = Path(name)
    return p if p.is_absolute() else config_path.parent / p


class ConfigStore:
    """Reads and rewrites config.json / secrets.json. Holds the path to the config file."""

    def __init__(self, config_path: str | os.PathLike):
        self.config_path = Path(config_path)

    def _read_raw(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def _read_secrets(self, config: AppConfig) -> dict:
        sp = _secrets_path(self.config_path, config)
        if sp.exists():
            return json.loads(sp.read_text(encoding="utf-8"))
        return {}

    def _save(self, raw: dict, secrets: dict, config: AppConfig) -> AppConfig:
        # 1. Verify the new config is valid — BEFORE writing to disk
        try:
            AppConfig.model_validate(raw)
        except Exception as exc:
            raise ConfigError(f"Configuration failed validation: {exc}")
        # 2. Write both files atomically
        _atomic_write(self.config_path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
        sp = _secrets_path(self.config_path, config)
        _atomic_write(sp, json.dumps(secrets, ensure_ascii=False, indent=2) + "\n")
        try:
            if os.name == "posix":
                os.chmod(sp, 0o600)
        except OSError:
            pass
        # 3. Return a freshly loaded config (with secrets applied)
        return load_config(self.config_path)

    def add_host(self, group_name: str, host_data: dict, secret_data: dict) -> AppConfig:
        raw = self._read_raw()
        config = load_config(self.config_path)
        secrets = self._read_secrets(config)

        host_id = host_data.get("id", "").strip()
        if not host_id:
            raise ConfigError("Host id is not set")
        if any(h["id"] == host_id for g in raw["groups"] for h in g["hosts"]):
            raise ConfigError(f"A host with id \"{host_id}\" already exists")

        # find or create the group
        group = next((g for g in raw["groups"] if g["name"] == group_name), None)
        if group is None:
            orders = [g.get("order", 0) for g in raw["groups"]]
            group = {"name": group_name, "order": (max(orders) + 1) if orders else 1,
                     "mode": "parallel", "on_failure": "continue", "hosts": []}
            raw["groups"].append(group)
        group["hosts"].append(self._clean_host(host_data))
        self._apply_secret(secrets, host_id, secret_data)
        return self._save(raw, secrets, config)

    def update_host(self, host_id: str, host_data: dict, secret_data: dict) -> AppConfig:
        raw = self._read_raw()
        config = load_config(self.config_path)
        secrets = self._read_secrets(config)

        found = False
        for g in raw["groups"]:
            for i, h in enumerate(g["hosts"]):
                if h["id"] == host_id:
                    new = self._clean_host({**host_data, "id": host_id})
                    g["hosts"][i] = new
                    found = True
        if not found:
            raise ConfigError(f"Host \"{host_id}\" not found")
        self._apply_secret(secrets, host_id, secret_data)
        return self._save(raw, secrets, config)

    def delete_host(self, host_id: str) -> AppConfig:
        raw = self._read_raw()
        config = load_config(self.config_path)
        secrets = self._read_secrets(config)

        found = False
        for g in raw["groups"]:
            before = len(g["hosts"])
            g["hosts"] = [h for h in g["hosts"] if h["id"] != host_id]
            found = found or len(g["hosts"]) < before
        if not found:
            raise ConfigError(f"Host \"{host_id}\" not found")
        # remove empty groups
        raw["groups"] = [g for g in raw["groups"] if g["hosts"]]
        if not raw["groups"]:
            raise ConfigError("Cannot delete the last host: the configuration must contain at least one")
        secrets.pop(host_id, None)
        return self._save(raw, secrets, config)

    @staticmethod
    def _clean_host(data: dict) -> dict:
        """Keep only host fields, drop empty values; secrets never go here."""
        allowed = set(Host.model_fields) - {"credentials"}
        out: dict[str, Any] = {}
        for key in allowed:
            if key in data and data[key] not in (None, "", []):
                out[key] = data[key]
        if "id" in data:
            out["id"] = data["id"]
        return out

    @staticmethod
    def _apply_secret(secrets: dict, host_id: str, secret_data: dict) -> None:
        entry = {k: v for k, v in (secret_data or {}).items() if k in SECRET_KEYS and v}
        if entry:
            secrets[host_id] = entry
