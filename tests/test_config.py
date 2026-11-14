import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import AppConfig, load_config

from .conftest import make_config


def test_example_config_is_valid():
    cfg = load_config(Path(__file__).parent.parent / "config.example.json")
    assert cfg.all_hosts()


def test_defaults_by_os():
    cfg = make_config()
    host = cfg.get_host("db-01")
    assert host.port == 22
    assert host.app_service_type == "systemd"


def test_duplicate_ids_rejected():
    data = json.loads(make_config().model_dump_json())
    data["groups"][1]["hosts"][0]["id"] = "db-01"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_wrong_service_type_for_os_rejected():
    data = json.loads(make_config().model_dump_json())
    data["groups"][0]["hosts"][0]["app_service_type"] = "windows_service"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_public_view_has_no_credentials():
    view = json.dumps(make_config().public_view())
    assert "credentials" not in view and "password" not in view
