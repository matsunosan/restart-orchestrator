"""Encryption of machine passwords with a master password.

Scheme:
  * master password -> encryption key via scrypt (a slow KDF, resistant to brute force);
  * each machine password is encrypted with AES-256-GCM (encryption + integrity check);
  * to verify the entered master password is correct, a "verifier" is stored — a known string
    encrypted in advance; if it decrypts correctly -> the password is correct.

Encrypted value format (string):
    enc:v1:<base64(salt)>:<base64(nonce)>:<base64(ciphertext+tag)>

Plaintext (unencrypted) values in secrets.json are kept as-is — this is the development
mode without a master password. Encryption is enabled only once a master password is set.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

ENC_PREFIX = "enc:v1:"
VERIFIER_PLAINTEXT = b"restart-orchestrator-master-key-ok"

# scrypt parameters: a balance of strength and speed (~0.1 s per key derivation)
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32  # AES-256


class CryptoError(Exception):
    pass


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def _derive_key(master_password: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=_KEY_LEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return kdf.derive(master_password.encode("utf-8"))


def encrypt(master_password: str, plaintext: str) -> str:
    """Encrypt a string with the master password. Each call uses its own salt and nonce."""
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(master_password, salt)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return f"{ENC_PREFIX}{_b64e(salt)}:{_b64e(nonce)}:{_b64e(ct)}"


def decrypt(master_password: str, token: str) -> str:
    """Decrypt a string. A wrong master password or tampering -> CryptoError."""
    if not is_encrypted(token):
        raise CryptoError("value is not encrypted")
    try:
        _, _, rest = token.partition(ENC_PREFIX)
        salt_b64, nonce_b64, ct_b64 = rest.split(":")
        salt, nonce, ct = _b64d(salt_b64), _b64d(nonce_b64), _b64d(ct_b64)
    except (ValueError, Exception) as exc:
        raise CryptoError(f"corrupted encrypted value: {exc}") from exc
    key = _derive_key(master_password, salt)
    try:
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except InvalidTag as exc:
        raise CryptoError("wrong master password or the value is corrupted") from exc


def make_verifier(master_password: str) -> str:
    """Create a "verifier" to check the master password on subsequent runs."""
    return encrypt(master_password, VERIFIER_PLAINTEXT.decode("utf-8"))


def check_verifier(master_password: str, verifier: str) -> bool:
    """Check that the master password matches the one the verifier was created with."""
    try:
        return decrypt(master_password, verifier).encode("utf-8") == VERIFIER_PLAINTEXT
    except CryptoError:
        return False


@dataclass
class MasterKeyState:
    """In-memory master-key state: the password itself and when it was entered."""
    master_password: str
    unlocked_at: float
