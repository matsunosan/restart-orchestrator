"""Web application: REST API + Server-Sent Events + static UI.

Run: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
(strictly 1 worker: state and queue live in the process memory).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys as _sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic

from .config import load_config
from .config_editor import ConfigStore
from .console import setup_console
from .logging_setup import setup_logging
from .monitoring import PingMonitor, ZabbixMonitor
from .orchestrator import BusyError, Orchestrator
from .selfdetect import find_self_hosts
from .sessions import SessionStore
from .state import StateStore
from .vault import Vault

if getattr(_sys, "frozen", False):
    BASE_DIR = Path(_sys._MEIPASS) / "app"  # unpacked datas in the built exe
else:
    BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("orchestrator")
basic = HTTPBasic(auto_error=False)
SESSIONS = SessionStore()
SESSION_COOKIE = "ro_session"


# ---------- security ----------
def require_auth(request: Request) -> None:
    """Access via a session cookie. For the API: 401 if there is no session."""
    if SESSIONS.auth_disabled():
        return
    user, password = SESSIONS.credentials()
    if not user or not password:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Set the ADMIN_USER and ADMIN_PASSWORD environment variables")
    if not SESSIONS.valid(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Login required")


def require_admin(request: Request) -> None:
    """Machine-editing operations — admin role only."""
    if SESSIONS.auth_disabled():
        return
    if SESSIONS.role(request.cookies.get(SESSION_COOKIE)) != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator only")


def require_ui_header(x_requested_with: str | None = Header(None)) -> None:
    """CSRF protection: a foreign site cannot send a POST with a custom header without CORS."""
    if x_requested_with != "restart-ui":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing header X-Requested-With: restart-ui")


# ---------- lifecycle ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_console()
    import os as _os
    _level = logging.DEBUG if _os.environ.get("LOG_LEVEL", "").upper() == "DEBUG" else logging.INFO
    config_path = os.environ.get("CONFIG_PATH", "config.json")
    config = load_config(config_path)
    setup_logging(config.settings.log_file, level=_level)
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=max(32, config.settings.max_parallel_hosts * 2),
                                                 thread_name_prefix="executor"))
    app.state.config_store = ConfigStore(config_path)
    await _activate_config(app, config)
    mode = "DRY-RUN (simulation, real machines are not touched)" if config.settings.dry_run else "LIVE"
    app.state.store.event("info", f"Orchestrator started: {len(config.all_hosts())} hosts, mode {mode}")
    for warning in config.load_warnings:
        app.state.store.event("warning", f"Configuration: {warning}")
    try:
        yield
    finally:
        await _stop_monitors(app)
        await app.state.orchestrator.shutdown()


async def _stop_monitors(app: FastAPI) -> None:
    for task in getattr(app.state, "monitors", []):
        task.cancel()
    await asyncio.gather(*getattr(app.state, "monitors", []), return_exceptions=True)


async def _activate_config(app: FastAPI, config) -> None:
    """Build (or rebuild) state, orchestrator and monitors for a new config."""
    store = StateStore(config)
    secrets_path = config.settings.secrets_file or "secrets.json"
    import os as _os2
    if not _os2.path.isabs(secrets_path):
        base = _os2.path.dirname(_os2.environ.get("CONFIG_PATH", "config.json")) or "."
        secrets_path = _os2.path.join(base, secrets_path)
    # keep the vault across config rebuilds so unlocking is not lost on edits
    vault = getattr(app.state, "vault", None)
    if vault is None:
        vault = Vault(secrets_path, clear_after_seconds=config.settings.master_key_clear_after_seconds)
        app.state.vault = vault
    orchestrator = Orchestrator(config, store, vault)
    app.state.store = store
    app.state.orchestrator = orchestrator
    app.state.config = config
    app.state.monitors = [
        asyncio.create_task(PingMonitor(config, store).run()),
        asyncio.create_task(ZabbixMonitor(config, store).run()),
    ]
    orchestrator.set_excluded(await asyncio.to_thread(find_self_hosts, config))


app = FastAPI(title="Restart Orchestrator", lifespan=lifespan)
api = APIRouter(dependencies=[Depends(require_auth)])


def _orch(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


def _store(request: Request) -> StateStore:
    return request.app.state.store


# ---------- routes ----------
@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"status": "ok"}





@api.get("/api/state")
async def get_state(request: Request) -> dict:
    snap = _store(request).snapshot()
    v = request.app.state.vault
    snap["vault"] = {"status": v.status(), "seconds_left": v.seconds_left()}
    role = SESSIONS.role(request.cookies.get(SESSION_COOKIE)) or ("admin" if SESSIONS.auth_disabled() else "se")
    snap["role"] = role
    return snap


@api.post("/api/stack/restart", status_code=202, dependencies=[Depends(require_ui_header)])
async def restart_stack(request: Request) -> dict:
    # optional: {"host_ids": [...]} — restart only the selected ones; without it — all
    only_ids = None
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("host_ids"), list):
            only_ids = [str(x) for x in body["host_ids"]]
    except Exception:
        pass
    try:
        _orch(request).start_stack(only_ids)
    except BusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return {"status": "started", "count": len(only_ids) if only_ids is not None else None}


@api.post("/api/stack/cancel", status_code=202, dependencies=[Depends(require_ui_header)])
async def cancel_stack(request: Request) -> dict:
    try:
        _orch(request).request_cancel()
    except BusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return {"status": "cancel_requested"}


@api.post("/api/hosts/{host_id}/restart", status_code=202, dependencies=[Depends(require_ui_header)])
async def restart_host(host_id: str, request: Request) -> dict:
    try:
        _orch(request).start_host(host_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Host {host_id} not found")
    except BusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return {"status": "started", "host_id": host_id}


@api.get("/api/hosts/{host_id}/edit")
async def host_for_edit(host_id: str, request: Request) -> dict:
    try:
        return request.app.state.config_store.host_for_edit(host_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Host {host_id} not found")


@api.get("/api/groups")
async def list_groups(request: Request) -> dict:
    return {"groups": request.app.state.config_store.list_groups()}


async def _edit_config(request: Request, action: str, payload: dict) -> dict:
    orch = _orch(request)
    # the config must not change during an active restart — in-memory state would desync
    if orch.stack_running or orch._active:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "A restart is running. Wait for it to finish before changing the machine list")
    try:
        new_config = await asyncio.to_thread(request.app.state.config_store.validate_and_apply, action, payload)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    # rebuild state for the new config
    await _stop_monitors(request.app)
    await _activate_config(request.app, new_config)
    request.app.state.store.event("info", f"Configuration changed ({action}), hosts: {len(new_config.all_hosts())}")
    return {"status": "ok", "hosts": len(new_config.all_hosts())}


@api.post("/api/hosts", status_code=201, dependencies=[Depends(require_ui_header), Depends(require_admin)])
async def add_host(request: Request) -> dict:
    return await _edit_config(request, "add", await request.json())


@api.put("/api/hosts/{host_id}", dependencies=[Depends(require_ui_header), Depends(require_admin)])
async def update_host(host_id: str, request: Request) -> dict:
    payload = await request.json()
    payload["id"] = host_id  # the id from the path is the source of truth
    return await _edit_config(request, "update", payload)


@api.delete("/api/hosts/{host_id}", dependencies=[Depends(require_ui_header), Depends(require_admin)])
async def delete_host(host_id: str, request: Request) -> dict:
    return await _edit_config(request, "delete", {"id": host_id})


@api.post("/api/check", status_code=202, dependencies=[Depends(require_ui_header)])
async def check_all(request: Request) -> dict:
    try:
        started = _orch(request).start_check()
    except BusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return {"status": "started", "host_ids": started}


@api.post("/api/hosts/{host_id}/check", status_code=202, dependencies=[Depends(require_ui_header)])
async def check_host(host_id: str, request: Request) -> dict:
    try:
        _orch(request).start_check([host_id])
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Host {host_id} not found")
    except BusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return {"status": "started", "host_id": host_id}


@api.get("/api/vault")
async def vault_status(request: Request) -> dict:
    v = request.app.state.vault
    return {"status": v.status(), "seconds_left": v.seconds_left()}


@api.post("/api/vault/unlock", dependencies=[Depends(require_ui_header)])
async def vault_unlock(request: Request) -> dict:
    body = await request.json()
    master = body.get("master_password", "")
    if request.app.state.vault.unlock(master):
        request.app.state.store.event("info", "Master password entered, machine passwords unlocked")
        return {"status": "unlocked"}
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong master password")


@api.post("/api/vault/lock", dependencies=[Depends(require_ui_header)])
async def vault_lock(request: Request) -> dict:
    request.app.state.vault.lock()
    request.app.state.store.event("info", "Master key wiped from memory (locked)")
    return {"status": "locked"}


@api.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    store = _store(request)
    queue = store.subscribe()

    def sse(kind: str, data) -> str:
        return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

    async def generator():
        try:
            snap0 = store.snapshot()
            snap0["vault"] = {"status": request.app.state.vault.status(),
                              "seconds_left": request.app.state.vault.seconds_left()}
            snap0["role"] = (SESSIONS.role(request.cookies.get(SESSION_COOKIE))
                             or ("admin" if SESSIONS.auth_disabled() else "se"))
            yield sse("snapshot", snap0)
            while not await request.is_disconnected():
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield sse(msg["type"], msg["data"])
        finally:
            store.unsubscribe(queue)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/login", include_in_schema=False)
async def login_page() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "login.html")


@app.post("/api/login")
async def do_login(request: Request) -> JSONResponse:
    if SESSIONS.auth_disabled():
        return JSONResponse({"status": "ok"})
    body = await request.json()
    user = str(body.get("user", ""))
    password = str(body.get("password", ""))
    role = SESSIONS.check_login(user, password)
    if not role:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password")
    token = SESSIONS.create(role)
    resp = JSONResponse({"status": "ok"})
    secure = request.url.scheme == "https"
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict",
                    secure=secure, max_age=SESSIONS.ttl)
    return resp


@app.post("/api/logout")
async def do_logout(request: Request) -> JSONResponse:
    SESSIONS.destroy(request.cookies.get(SESSION_COOKIE))
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/", include_in_schema=False)
async def index(request: Request):
    if not SESSIONS.auth_disabled() and not SESSIONS.valid(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/login")
    return FileResponse(BASE_DIR / "static" / "index.html")


app.include_router(api)
