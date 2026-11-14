"""Status visualizer: ICMP ping + management port check + Zabbix API."""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
import random
import sys
import time
from typing import Any

import httpx

from .config import AppConfig, Host
from .executors.simulated import is_sim_host_up
from .state import StateStore

log = logging.getLogger(__name__)

ZABBIX_SEVERITY = {0: "not classified", 1: "information", 2: "warning",
                   3: "average", 4: "high", 5: "disaster"}


class PingMonitor:
    def __init__(self, config: AppConfig, store: StateStore):
        self.config = config
        self.store = store

    @staticmethod
    async def icmp(hostname: str) -> tuple[bool | None, float | None]:
        """(reachable, rtt_ms). None — the ping utility is unavailable on the system."""
        if sys.platform.startswith("win"):
            args = ["ping", "-n", "1", "-w", "1000", hostname]
        elif sys.platform == "darwin":
            args = ["ping", "-c", "1", "-t", "2", hostname]
        else:
            args = ["ping", "-c", "1", "-W", "2", hostname]
        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        except (FileNotFoundError, PermissionError):
            return None, None
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            return False, None
        rtt = (time.perf_counter() - started) * 1000
        return (rc == 0), (round(rtt, 1) if rc == 0 else None)

    @staticmethod
    async def tcp(hostname: str, port: int) -> bool:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(hostname, port), timeout=3)
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True

    async def check(self, host: Host) -> None:
        if self.config.settings.dry_run:
            up = is_sim_host_up(host.id)
            self.store.set_monitor(host.id, online=up, icmp=up, mgmt_port=up,
                                   rtt_ms=round(random.uniform(0.4, 3), 1) if up else None,
                                   checked_at=time.time())
            return
        (icmp_ok, rtt), port_ok = await asyncio.gather(self.icmp(host.hostname), self.tcp(host.hostname, host.port))
        self.store.set_monitor(host.id, online=bool(icmp_ok) or port_ok, icmp=icmp_ok,
                               mgmt_port=port_ok, rtt_ms=rtt, checked_at=time.time())

    async def run(self) -> None:
        interval = self.config.settings.ping_interval_seconds
        while True:
            try:
                await asyncio.gather(*(self.check(h) for h in self.config.all_hosts()))
            except Exception:
                log.exception("Ping monitoring error")
            await asyncio.sleep(interval)


class ZabbixError(Exception):
    pass


class ZabbixMonitor:
    """Reads agent availability, active problems and uptime from Zabbix.

    Compatible with Zabbix 6.0+ (API-token auth in the Authorization: Bearer header).
    The host in Zabbix is looked up by its technical name (the host field) = zabbix_host or hostname from config.json.
    """

    def __init__(self, config: AppConfig, store: StateStore):
        self.config = config
        self.store = store
        self.settings = config.settings.zabbix
        self.token = os.environ.get(self.settings.api_token_env)
        self._ids = itertools.count(1)

    async def _call(self, client: httpx.AsyncClient, method: str, params: dict) -> Any:
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": next(self._ids)}
        response = await client.post(
            self.settings.url, json=payload,
            headers={"Content-Type": "application/json-rpc", "Authorization": f"Bearer {self.token}"})
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            err = data["error"]
            raise ZabbixError(f"{method}: {err.get('message')} {err.get('data', '')}")
        return data["result"]

    async def poll_once(self, client: httpx.AsyncClient) -> None:
        name_to_id = {(h.zabbix_host or h.hostname): h.id for h in self.config.all_hosts()}
        zhosts = await self._call(client, "host.get", {
            "output": ["hostid", "host", "name", "status", "maintenance_status"],
            "filter": {"host": list(name_to_id)},
            "selectInterfaces": ["type", "available", "error"],
        })
        by_zid = {z["hostid"]: z for z in zhosts}
        zids = list(by_zid)

        problems: dict[str, list] = {zid: [] for zid in zids}
        uptime: dict[str, int] = {}
        if zids:
            triggers, items = await asyncio.gather(
                self._call(client, "trigger.get", {
                    "hostids": zids, "output": ["triggerid", "description", "priority"],
                    "filter": {"value": 1}, "monitored": True, "skipDependent": True,
                    "expandDescription": True, "selectHosts": ["hostid"],
                }),
                self._call(client, "item.get", {
                    "hostids": zids, "output": ["hostid", "lastvalue", "lastclock"],
                    "filter": {"key_": "system.uptime"},
                }),
            )
            for trig in triggers:
                for h in trig.get("hosts", []):
                    problems.setdefault(h["hostid"], []).append({
                        "description": trig["description"],
                        "severity": ZABBIX_SEVERITY.get(int(trig["priority"]), trig["priority"]),
                        "priority": int(trig["priority"]),
                    })
            for item in items:
                try:
                    uptime[item["hostid"]] = int(float(item["lastvalue"]))
                except (TypeError, ValueError):
                    pass

        found: set[str] = set()
        for zid, zhost in by_zid.items():
            host_id = name_to_id.get(zhost["host"])
            if not host_id:
                continue
            found.add(host_id)
            avail = {int(i.get("available", 0)) for i in zhost.get("interfaces", [])}
            agent = "up" if 1 in avail else "down" if 2 in avail else "unknown"
            self.store.set_monitor(host_id, zabbix={
                "state": "ok",
                "agent": agent,
                "monitored": zhost["status"] == "0",
                "maintenance": zhost.get("maintenance_status") == "1",
                "problems": sorted(problems.get(zid, []), key=lambda p: -p["priority"])[:10],
                "uptime_seconds": uptime.get(zid),
                "checked_at": time.time(),
            })
        for name, host_id in name_to_id.items():
            if host_id not in found:
                self.store.set_monitor(host_id, zabbix={"state": "not_found", "error": f"Host \"{name}\" not found in Zabbix"})

    async def run(self) -> None:
        if not self.settings.enabled:
            return
        if not self.token:
            self.store.event("warning", f"Zabbix is enabled, but variable {self.settings.api_token_env} is not set")
            return
        error_reported = False
        async with httpx.AsyncClient(timeout=15, verify=self.settings.verify_ssl) as client:
            while True:
                try:
                    await self.poll_once(client)
                    if error_reported:
                        self.store.event("info", "Connection to Zabbix restored")
                    error_reported = False
                except Exception as exc:
                    log.warning("Zabbix unavailable: %s", exc)
                    if not error_reported:
                        self.store.event("warning", f"Zabbix unavailable: {exc}")
                        error_reported = True
                    for host in self.config.all_hosts():
                        self.store.set_monitor(host.id, zabbix={"state": "error", "error": str(exc)[:200]})
                await asyncio.sleep(self.settings.poll_interval_seconds)
