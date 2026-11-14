"""In-memory host and stack state + change broadcasting to subscribers (SSE)."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .config import AppConfig

log = logging.getLogger("orchestrator")


class RunStatus(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    PRECHECK = "precheck"
    REBOOTING = "rebooting"
    WAITING_BOOT = "waiting_boot"
    STARTING_APP = "starting_app"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


ACTIVE_STATUSES = {RunStatus.PRECHECK, RunStatus.REBOOTING, RunStatus.WAITING_BOOT, RunStatus.STARTING_APP}
TERMINAL_STATUSES = {RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.SKIPPED, RunStatus.CANCELLED}


@dataclass
class HostRuntime:
    id: str
    status: RunStatus = RunStatus.IDLE
    message: str = ""
    failure_stage: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    updated_at: float = field(default_factory=time.time)
    monitor: dict[str, Any] = field(default_factory=dict)
    excluded: str | None = None
    last_check: dict[str, Any] | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data


class StateStore:
    def __init__(self, config: AppConfig):
        self.config = config
        self.hosts: dict[str, HostRuntime] = {h.id: HostRuntime(h.id) for h in config.all_hosts()}
        self.events: deque[dict] = deque(maxlen=500)
        self.stack: dict[str, Any] = {
            "running": False,
            "status": "idle",
            "current_group": None,
            "started_at": None,
            "finished_at": None,
            "cancel_requested": False,
            "message": "",
        }
        self._subscribers: set[asyncio.Queue] = set()

    # ---------- subscriptions ----------
    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def _publish(self, kind: str, data: Any) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait({"type": kind, "data": data})
            except asyncio.QueueFull:
                pass  # a slow client misses the event and gets a snapshot on reconnect

    # ---------- changes ----------
    def set_status(self, host_id: str, status: RunStatus, message: str = "",
                   failure_stage: str | None = None) -> None:
        rt = self.hosts[host_id]
        now = time.time()
        if status == RunStatus.QUEUED:
            rt.started_at = None
            rt.finished_at = None
            rt.failure_stage = None
        if status == RunStatus.PRECHECK:
            rt.started_at = now
            rt.finished_at = None
            rt.failure_stage = None
        if status in TERMINAL_STATUSES:
            rt.finished_at = now
        if failure_stage:
            rt.failure_stage = failure_stage
        rt.status = status
        rt.message = message
        rt.updated_at = now
        self._publish("host", rt.to_dict())

    def set_monitor(self, host_id: str, **values: Any) -> None:
        rt = self.hosts[host_id]
        rt.monitor.update(values)
        self._publish("host", rt.to_dict())

    def set_excluded(self, host_id: str, reason: str) -> None:
        self.hosts[host_id].excluded = reason
        self._publish("host", self.hosts[host_id].to_dict())

    def set_check(self, host_id: str, result: dict[str, Any]) -> None:
        self.hosts[host_id].last_check = result
        self._publish("host", self.hosts[host_id].to_dict())

    def set_stack(self, **values: Any) -> None:
        self.stack.update(values)
        self._publish("stack", dict(self.stack))

    def event(self, level: str, message: str, host_id: str | None = None) -> None:
        entry = {"ts": time.time(), "level": level, "message": message, "host_id": host_id}
        self.events.append(entry)
        log_level = {"success": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO)
        log.log(log_level, "%s%s", f"[{host_id}] " if host_id else "", message)
        self._publish("event", entry)

    def snapshot(self) -> dict:
        s = self.config.settings
        return {
            "settings": {
                "dry_run": s.dry_run,
                "zabbix_enabled": s.zabbix.enabled,
            },
            "groups": self.config.public_view(),
            "hosts": {hid: rt.to_dict() for hid, rt in self.hosts.items()},
            "stack": dict(self.stack),
            "events": list(self.events)[-200:],
        }
