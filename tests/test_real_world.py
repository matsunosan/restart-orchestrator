"""Self-exclusion, passwords from secrets.json, autologon, and check without reboot."""
import asyncio
import json

from app.config import load_config
from app.orchestrator import BusyError, Orchestrator
from app.selfdetect import find_self_hosts
from app.state import RunStatus, StateStore

from .conftest import make_config


def _with_localhost():
    data = json.loads(make_config().model_dump_json())
    data["groups"][1]["hosts"][1]["hostname"] = "localhost"  # app-02 = "the orchestrator server"
    return data


def test_self_host_detected():
    from app.config import AppConfig
    cfg = AppConfig.model_validate(_with_localhost())
    excluded = find_self_hosts(cfg)
    assert "app-02" in excluded and "db-01" not in excluded


def test_manual_exclusion():
    cfg = make_config(exclude_host_ids=["db-01"], self_detection=False)
    assert set(find_self_hosts(cfg)) == {"db-01"}


def test_stack_skips_self_and_rejects_manual(fast_sim):
    from app.config import AppConfig

    async def scenario():
        cfg = AppConfig.model_validate(_with_localhost())
        store = StateStore(cfg)
        orch = Orchestrator(cfg, store)
        orch.set_excluded(find_self_hosts(cfg))
        try:
            orch.start_host("app-02")
            raise AssertionError("the orchestrator server must not be rebootable")
        except BusyError:
            pass
        orch.start_stack()
        while orch.stack_running:
            await asyncio.sleep(0.1)
        return store

    store = asyncio.run(scenario())
    assert store.stack["status"] == "success"
    assert store.hosts["app-02"].status == RunStatus.SKIPPED
    assert store.hosts["app-01"].status == RunStatus.SUCCESS


def test_secrets_file(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(make_config(secrets_file="secrets.json").model_dump_json())
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps({"db-01": {"sudo_password": "s1"}, "app-01": {"password": "p2"}, "ghost": {}}))
    secrets.chmod(0o644)
    cfg = load_config(cfg_path)
    assert cfg.get_host("db-01").credentials.get_sudo_password() == "s1"
    assert cfg.get_host("app-01").credentials.get_password() == "p2"
    assert cfg.get_host("app-02").credentials.get_password() is None
    assert any("chmod 600" in w for w in cfg.load_warnings)
    assert any("ghost" in w for w in cfg.load_warnings)
    assert "s1" not in json.dumps(cfg.public_view())


def test_env_overrides_secrets_file(tmp_path, monkeypatch):
    data = json.loads(make_config().model_dump_json())
    data["groups"][0]["hosts"][0]["credentials"] = {"sudo_password_env": "DB_SUDO"}
    (tmp_path / "config.json").write_text(json.dumps(data))
    (tmp_path / "secrets.json").write_text(json.dumps({"db-01": {"sudo_password": "from-file"}}))
    monkeypatch.setenv("DB_SUDO", "from-env")
    assert load_config(tmp_path / "config.json").get_host("db-01").credentials.get_sudo_password() == "from-env"


def test_user_session_host(fast_sim):
    data = json.loads(make_config().model_dump_json())
    data["groups"][1]["hosts"][0].update(os="windows", app_service_type="windows_task",
                                         requires_user_session=True, session_user="EXAMPLE\\kiosk")
    from app.config import AppConfig
    cfg = AppConfig.model_validate(data)

    async def scenario():
        store = StateStore(cfg)
        orch = Orchestrator(cfg, store)
        orch.start_stack()
        while orch.stack_running:
            await asyncio.sleep(0.1)
        return store

    store = asyncio.run(scenario())
    assert store.hosts["app-01"].status == RunStatus.SUCCESS
    assert any("logged in" in e["message"] for e in store.events)


def test_check_does_not_reboot(fast_sim):
    cfg = make_config()

    async def scenario():
        store = StateStore(cfg)
        orch = Orchestrator(cfg, store)
        return await orch.check_host(cfg.get_host("db-01"))

    result = asyncio.run(scenario())
    assert result["state"] == "ok"
    assert fast_sim._SIM["db-01"].boot_id == 1  # there was no reboot


def test_linux_kiosk_desktop(fast_sim):
    """Linux kiosk: graphical autostart, process check, no systemd, no manual start."""
    data = json.loads(make_config().model_dump_json())
    h = data["groups"][0]["hosts"][0]
    h.update(app_service_type="desktop_autostart", app_service_name="webui",
             app_process_name="firefox", requires_user_session=True, session_user="kiosk")
    # the second host is a regular one so the stack is not empty
    from app.config import AppConfig
    cfg = AppConfig.model_validate(data)

    async def scenario():
        store = StateStore(cfg)
        orch = Orchestrator(cfg, store)
        return await orch.check_host(cfg.get_host("db-01"))

    result = asyncio.run(scenario())
    assert result["state"] == "ok"
    labels = {i["label"] for i in result["items"]}
    assert "User session" in labels


def test_desktop_autostart_requires_fields():
    import pytest
    from pydantic import ValidationError
    data = json.loads(make_config().model_dump_json())
    data["groups"][0]["hosts"][0].update(app_service_type="desktop_autostart", requires_user_session=True)
    # no app_process_name → error
    with pytest.raises(ValidationError):
        from app.config import AppConfig
        AppConfig.model_validate(data)
