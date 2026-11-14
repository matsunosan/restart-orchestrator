import pytest

from app.config import AppConfig


def make_config(**settings) -> AppConfig:
    base = {
        "dry_run": True,
        "poll_interval_seconds": 1,
        "app_retry_delay_seconds": 1,
        "app_start_retries": 1,
        "log_file": "/tmp/restart-orchestrator-test.log",
    }
    base.update(settings)
    host = {"os": "linux", "user": "u", "timeout_seconds": 10,
            "app_start_timeout_seconds": 5, "app_service_name": "myapp.service"}
    return AppConfig.model_validate({
        "settings": base,
        "groups": [
            {"name": "db", "order": 1, "on_failure": "halt",
             "hosts": [{**host, "id": "db-01", "name": "DB", "hostname": "10.0.0.1"}]},
            {"name": "app", "order": 2, "mode": "parallel",
             "hosts": [{**host, "id": "app-01", "name": "App1", "hostname": "10.0.0.2"},
                       {**host, "id": "app-02", "name": "App2", "hostname": "10.0.0.3"}]},
        ],
    })


@pytest.fixture
def fast_sim(monkeypatch):
    """Simulation without pauses: the host "reboots" instantly."""
    import app.executors.simulated as sim
    sim._SIM.clear()
    monkeypatch.setattr(sim.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(sim.time, "sleep", lambda s: None)
    return sim
