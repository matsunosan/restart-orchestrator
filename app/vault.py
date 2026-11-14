"""In-memory master-key store and decryption of machine passwords.

Modes:
  * ENCRYPTION OFF (development): secrets.json has no verifier (_verifier) and values are
    plaintext. Passwords are read as-is, no master password is needed. This is the default mode.
  * ENCRYPTION ON (production): secrets.json has a _verifier and values are encrypted.
    A machine password is available only after the master password is entered (unlock). After
    clear_after_seconds of inactivity the master key is wiped from memory and passwords become unavailable again.

Lock statuses:
  * "disabled"  — encryption is off, everything is open;
  * "locked"    — encryption is on, the master password has not been entered yet (Windows unavailable);
  * "unlocked"  — the master password has been entered, passwords are available.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import crypto

VERIFIER_KEY = "_verifier"


class VaultLocked(Exception):
    """A machine password is encrypted, but the master key is not entered or has expired."""


class Vault:
    def __init__(self, secrets_path: str | Path, clear_after_seconds: int = 7200):
        self.secrets_path = Path(secrets_path)
        self.clear_after_seconds = clear_after_seconds
        self._master: str | None = None
        self._unlocked_at: float = 0.0
        self._lock = threading.Lock()

    # ---------- file state ----------
    def _read_secrets(self) -> dict:
        try:
            return json.loads(self.secrets_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    @property
    def encryption_enabled(self) -> bool:
        """Whether encryption is on — determined by the presence of the verifier in secrets.json."""
        return VERIFIER_KEY in self._read_secrets()

    # ---------- expiry ----------
    def _expired(self) -> bool:
        if self._master is None:
            return True
        if self.clear_after_seconds <= 0:
            return False
        return (time.time() - self._unlocked_at) > self.clear_after_seconds

    def _enforce_expiry(self) -> None:
        if self._master is not None and self._expired():
            self._master = None
            self._unlocked_at = 0.0

    # ---------- lock/unlock ----------
    def status(self) -> str:
        with self._lock:
            if not self.encryption_enabled:
                return "disabled"
            self._enforce_expiry()
            return "unlocked" if self._master is not None else "locked"

    def seconds_left(self) -> int | None:
        """Seconds until auto-wipe; None if not applicable."""
        with self._lock:
            if self._master is None or self.clear_after_seconds <= 0:
                return None
            left = self.clear_after_seconds - (time.time() - self._unlocked_at)
            return max(0, int(left))

    def unlock(self, master_password: str) -> bool:
        """Enter the master password. True — correct (unlocked), False — wrong."""
        with self._lock:
            secrets = self._read_secrets()
            verifier = secrets.get(VERIFIER_KEY)
            if not verifier:
                return False  # encryption is off — nothing to unlock
            if crypto.check_verifier(master_password, verifier):
                self._master = master_password
                self._unlocked_at = time.time()
                return True
            return False

    def lock(self) -> None:
        """Wipe the master key from memory manually."""
        with self._lock:
            self._master = None
            self._unlocked_at = 0.0

    def touch(self) -> None:
        """Extend the session (reset the timer) on activity."""
        with self._lock:
            if self._master is not None:
                self._unlocked_at = time.time()

    # ---------- password retrieval ----------
    def reveal(self, value: str | None) -> str | None:
        """Return the plaintext password from a secrets.json value.

        A plaintext value is returned as-is. An encrypted one is decrypted with the master key;
        if the key is missing or expired -> VaultLocked.
        """
        if value is None:
            return None
        if not crypto.is_encrypted(value):
            return value  # development mode: plaintext password
        with self._lock:
            self._enforce_expiry()
            if self._master is None:
                raise VaultLocked("Enter the master password to decrypt machine passwords")
            return crypto.decrypt(self._master, value)
