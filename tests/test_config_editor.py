"""Host editor: validation, saving, secret separation."""
import json

import pytest

from app.config_editor import ConfigStore, valid_hostname


def _store(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "settings": {"dry_run": True, "secrets_file": "secrets.json", "log_file": "logs/o.log"},
        "groups": [{"name": "g1", "order": 1, "mode": "parallel", "on_failure": "continue",
                    "hosts": [{"id": "h1", "name": "H1", "hostname": "10.0.0.1", "os": "linux",
                               "user": "u", "credentials": {"ssh_key": "~/.k"}}]}],
    }))
    return ConfigStore(cfg)


def test_hostname_validation():
    # valid
    for ok in ("192.168.1.1", "10.0.0.1", "host.example.local", "my-server", "  10.0.0.1  "):
        assert valid_hostname(ok), ok
    # invalid, including one from a real bug
    for bad in ("10.0.0.2404444", "10,0.0.204", "10.0.0", "256.1.1.1",
                "192.168.1.1.1", "999.999.1.1 bad", "not a host", "host_underscore", ""):
        assert not valid_hostname(bad), bad


@pytest.mark.parametrize("bad", [
    {"id": "x", "name": "X", "hostname": "10.0.0.2", "os": "linux", "user": "u", "group": "g", "ssh_key": "~/.k", "junk": 1},
    {"id": "a b", "name": "X", "hostname": "10.0.0.2", "os": "linux", "user": "u", "group": "g", "ssh_key": "~/.k"},
    {"id": "x", "name": "X", "hostname": "10.0.0.2", "os": "windows", "user": "u", "group": "g"},  # no password
    {"id": "x", "name": "X", "hostname": "10.0.0.2", "os": "linux", "user": "u", "group": "g"},  # no key/pw
    {"id": "x", "name": "X", "hostname": "10.0.0.2", "os": "linux", "user": "u", "group": "g", "ssh_key": "~/.k",
     "app_service_name": "s", "app_service_type": "windows_task"},  # wrong type
    {"id": "x", "name": "X", "hostname": "10.0.0.2", "os": "linux", "user": "u", "group": "g", "ssh_key": "~/.k",
     "app_health_port": 99999},
])
def test_invalid_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        _store(tmp_path).validate_and_apply("add", bad)


def test_add_update_delete_and_secrets(tmp_path):
    store = _store(tmp_path)
    store.validate_and_apply("add", {
        "id": "win1", "name": "Win", "hostname": "192.168.1.5", "os": "windows", "user": "d\\u",
        "group": "Windows", "password": "SECRET", "app_service_name": "App",
        "app_service_type": "windows_task", "app_health_port": 30020,
        "requires_user_session": True, "session_user": "d\\u"})
    cfg_text = (tmp_path / "config.json").read_text()
    secrets = json.loads((tmp_path / "secrets.json").read_text())
    assert "win1" in cfg_text
    assert "SECRET" not in cfg_text          # password not in the config
    assert secrets["win1"]["password"] == "SECRET"

    store.validate_and_apply("update", {
        "id": "win1", "name": "Win2", "hostname": "192.168.1.5", "os": "windows", "user": "d\\u",
        "group": "Windows", "password": "SECRET", "app_service_name": "App",
        "app_service_type": "windows_task", "app_health_port": 30010,
        "requires_user_session": True, "session_user": "d\\u"})
    raw = json.loads((tmp_path / "config.json").read_text())
    h = next(h for g in raw["groups"] for h in g["hosts"] if h["id"] == "win1")
    assert h["name"] == "Win2" and h["app_health_port"] == 30010

    store.validate_and_apply("delete", {"id": "win1"})
    raw = json.loads((tmp_path / "config.json").read_text())
    secrets = json.loads((tmp_path / "secrets.json").read_text())
    assert not any(h["id"] == "win1" for g in raw["groups"] for h in g["hosts"])
    assert "win1" not in secrets


def test_duplicate_id_rejected(tmp_path):
    with pytest.raises(ValueError, match="already exists"):
        _store(tmp_path).validate_and_apply("add", {
            "id": "h1", "name": "dup", "hostname": "10.0.0.9", "os": "linux", "user": "u",
            "group": "g1", "ssh_key": "~/.k"})


def test_host_for_edit_hides_password(tmp_path):
    store = _store(tmp_path)
    store.validate_and_apply("add", {
        "id": "w", "name": "W", "hostname": "10.0.0.3", "os": "windows", "user": "u",
        "group": "G", "password": "TOPSECRET"})
    info = store.host_for_edit("w")
    assert info["has_password"] is True
    assert "TOPSECRET" not in json.dumps(info)



def test_bad_ip_rejected_by_editor(tmp_path):
    with pytest.raises(ValueError, match="IP|host"):
        _store(tmp_path).validate_and_apply("add", {
            "id": "x", "name": "X", "hostname": "10.0.0.2404444", "os": "linux",
            "user": "u", "group": "g", "ssh_key": "~/.k"})
