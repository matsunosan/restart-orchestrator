import asyncio

from app.orchestrator import BusyError, Orchestrator
from app.state import RunStatus, StateStore

from .conftest import make_config


async def _run_stack(cfg):
    store = StateStore(cfg)
    orch = Orchestrator(cfg, store)
    orch.start_stack()
    for _ in range(300):
        await asyncio.sleep(0.1)
        if not orch.stack_running:
            break
    return store


def test_stack_success(fast_sim):
    store = asyncio.run(_run_stack(make_config()))
    assert store.stack["status"] == "success"
    assert all(h.status == RunStatus.SUCCESS for h in store.hosts.values())


def test_halt_on_app_failure(fast_sim, monkeypatch):
    # roll 0.6 with rate 0.5 => the host boots, but the app is "broken"
    monkeypatch.setattr(fast_sim.random, "random", lambda: 0.6)
    store = asyncio.run(_run_stack(make_config(dry_run_failure_rate=0.5)))
    assert store.stack["status"] == "halted"
    assert store.hosts["db-01"].status == RunStatus.FAILED
    assert store.hosts["db-01"].failure_stage == "app"
    assert store.hosts["app-01"].status == RunStatus.SKIPPED


def test_double_start_rejected(fast_sim):
    async def scenario():
        cfg = make_config()
        orch = Orchestrator(cfg, StateStore(cfg))
        orch.start_stack()
        try:
            orch.start_stack()
        except BusyError:
            pass
        else:
            raise AssertionError("a second stack start must be rejected")
        try:
            orch.start_host("db-01")
        except BusyError:
            pass
        else:
            raise AssertionError("a manual start during the stack run must be rejected")
        await orch.shutdown()
    asyncio.run(scenario())
