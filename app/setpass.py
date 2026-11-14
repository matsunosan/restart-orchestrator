"""Utility for configuring encryption and setting machine passwords.

    python -m app.setpass enable                 # enable encryption (set the master password)
    python -m app.setpass set <host_id>          # set/change a machine password (encrypt)
    python -m app.setpass set <host_id> --sudo   # set a machine's sudo password
    python -m app.setpass rekey                  # change the master password (re-encrypt everything)
    python -m app.setpass status                 # show the encryption state
    python -m app.setpass disable                # disable encryption (decrypt everything back)

The master password and machine passwords are entered hidden (not shown). The master password is
stored nowhere — you must remember it. If forgotten, machine passwords must be set again (disable
is then impossible; run enable again on top).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
from pathlib import Path

from . import crypto
from .config import load_config
from .vault import VERIFIER_KEY

SECRET_FIELDS = ("password", "sudo_password", "ssh_key_passphrase")


def _secrets_path(config_path: str) -> Path:
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    name = cfg.get("settings", {}).get("secrets_file", "secrets.json")
    p = Path(name)
    return p if p.is_absolute() else Path(config_path).parent / p


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    if os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _enabled(secrets: dict) -> bool:
    return VERIFIER_KEY in secrets


def _ask_master(prompt: str = "Master password: ") -> str:
    pw = getpass.getpass(prompt)
    if not pw:
        print("An empty master password is not allowed.")
        sys.exit(1)
    return pw


def _verify_master(secrets: dict) -> str:
    master = _ask_master()
    if not crypto.check_verifier(master, secrets[VERIFIER_KEY]):
        print("Wrong master password.")
        sys.exit(1)
    return master


def cmd_status(path: Path) -> None:
    secrets = _read(path)
    if not _enabled(secrets):
        print("Encryption: OFF. Machine passwords are stored in plaintext (development mode).")
        return
    print("Encryption: ON.")
    for host_id, entry in secrets.items():
        if host_id == VERIFIER_KEY:
            continue
        fields = ", ".join(f"{f}={'encrypted' if crypto.is_encrypted(v) else 'PLAINTEXT'}"
                           for f, v in entry.items() if f in SECRET_FIELDS)
        print(f"  {host_id}: {fields or '(no passwords)'}")


def cmd_enable(path: Path) -> None:
    secrets = _read(path)
    if _enabled(secrets):
        print("Encryption is already on. To change the master password: rekey.")
        return
    print("Enabling encryption. Set a master password (you will enter it in the web interface).")
    master = _ask_master("New master password: ")
    if _ask_master("Repeat master password: ") != master:
        print("Passwords do not match.")
        sys.exit(1)
    # encrypt all existing plaintext passwords
    count = 0
    for entry in secrets.values():
        for field in SECRET_FIELDS:
            if field in entry and not crypto.is_encrypted(entry[field]):
                entry[field] = crypto.encrypt(master, entry[field])
                count += 1
    secrets[VERIFIER_KEY] = crypto.make_verifier(master)
    _write(path, secrets)
    print(f"Encryption enabled. Passwords encrypted: {count}.")
    print("IMPORTANT: remember the master password. Without it, machine passwords cannot be recovered.")


def cmd_disable(path: Path) -> None:
    secrets = _read(path)
    if not _enabled(secrets):
        print("Encryption is already off.")
        return
    master = _verify_master(secrets)
    for host_id, entry in secrets.items():
        if host_id == VERIFIER_KEY:
            continue
        for field in SECRET_FIELDS:
            if field in entry and crypto.is_encrypted(entry[field]):
                entry[field] = crypto.decrypt(master, entry[field])
    secrets.pop(VERIFIER_KEY, None)
    _write(path, secrets)
    print("Encryption disabled. Passwords decrypted back to plaintext.")


def cmd_set(path: Path, host_id: str, sudo: bool, config_path: str) -> None:
    # verify the host exists in the config
    try:
        load_config(config_path).get_host(host_id)
    except KeyError:
        print(f"Host \"{host_id}\" not found in the configuration.")
        sys.exit(1)
    secrets = _read(path)
    field = "sudo_password" if sudo else "password"
    value = getpass.getpass(f"Password ({field}) for {host_id}: ")
    if not value:
        print("Empty password was not set.")
        sys.exit(1)
    if _enabled(secrets):
        master = _verify_master(secrets)
        value = crypto.encrypt(master, value)
        where = "encrypted"
    else:
        where = "saved in plaintext (encryption is off)"
    secrets.setdefault(host_id, {})[field] = value
    _write(path, secrets)
    print(f"Password {field} for {host_id} {where}.")


def cmd_rekey(path: Path) -> None:
    secrets = _read(path)
    if not _enabled(secrets):
        print("Encryption is off — run enable first.")
        return
    old = _verify_master(secrets)
    new = _ask_master("New master password: ")
    if _ask_master("Repeat new master password: ") != new:
        print("Passwords do not match.")
        sys.exit(1)
    for host_id, entry in secrets.items():
        if host_id == VERIFIER_KEY:
            continue
        for field in SECRET_FIELDS:
            if field in entry and crypto.is_encrypted(entry[field]):
                entry[field] = crypto.encrypt(new, crypto.decrypt(old, entry[field]))
    secrets[VERIFIER_KEY] = crypto.make_verifier(new)
    _write(path, secrets)
    print("Master password changed, all passwords re-encrypted.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Encryption and machine password management")
    parser.add_argument("--config", default=os.environ.get("CONFIG_PATH", "config.json"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("enable")
    sub.add_parser("disable")
    sub.add_parser("rekey")
    sp = sub.add_parser("set")
    sp.add_argument("host_id")
    sp.add_argument("--sudo", action="store_true", help="set the sudo password instead of the regular one")

    args = parser.parse_args()
    path = _secrets_path(args.config)
    if args.cmd == "status":
        cmd_status(path)
    elif args.cmd == "enable":
        cmd_enable(path)
    elif args.cmd == "disable":
        cmd_disable(path)
    elif args.cmd == "rekey":
        cmd_rekey(path)
    elif args.cmd == "set":
        cmd_set(path, args.host_id, args.sudo, args.config)


if __name__ == "__main__":
    main()
