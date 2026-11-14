"""Password encryption and the master key."""
import json
import time

import pytest

from app import crypto
from app.vault import VERIFIER_KEY, Vault, VaultLocked


def test_encrypt_roundtrip():
    t = crypto.encrypt("master", "secret")
    assert crypto.is_encrypted(t)
    assert crypto.decrypt("master", t) == "secret"


def test_wrong_password_rejected():
    t = crypto.encrypt("master", "secret")
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt("wrong", t)


def test_tampering_detected():
    t = crypto.encrypt("master", "secret")
    bad = t[:-4] + ("AAAA" if t[-4:] != "AAAA" else "BBBB")
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt("master", bad)


def test_verifier():
    v = crypto.make_verifier("master")
    assert crypto.check_verifier("master", v)
    assert not crypto.check_verifier("wrong", v)


def test_unique_ciphertext():
    assert crypto.encrypt("m", "same") != crypto.encrypt("m", "same")


def _vault(tmp_path, enabled=True, master="M", clear=7200):
    sec = tmp_path / "secrets.json"
    data = {"win-01": {"password": crypto.encrypt(master, "pw") if enabled else "pw"}}
    if enabled:
        data[VERIFIER_KEY] = crypto.make_verifier(master)
    sec.write_text(json.dumps(data))
    return Vault(sec, clear_after_seconds=clear), data


def test_vault_disabled(tmp_path):
    v, _ = _vault(tmp_path, enabled=False)
    assert v.status() == "disabled"
    assert v.reveal("plain") == "plain"


def test_vault_lock_unlock(tmp_path):
    v, data = _vault(tmp_path)
    enc = data["win-01"]["password"]
    assert v.status() == "locked"
    with pytest.raises(VaultLocked):
        v.reveal(enc)
    assert not v.unlock("wrong")
    assert v.unlock("M")
    assert v.status() == "unlocked"
    assert v.reveal(enc) == "pw"


def test_vault_expiry(tmp_path):
    v, data = _vault(tmp_path, clear=1)
    v.unlock("M")
    assert v.status() == "unlocked"
    time.sleep(1.2)
    assert v.status() == "locked"
    with pytest.raises(VaultLocked):
        v.reveal(data["win-01"]["password"])
