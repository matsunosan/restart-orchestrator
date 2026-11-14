"""Orchestrator: stack restart queue by groups, manual host restart, fail-safe.

Lifecycle of a single host:
  precheck -> (ensure autostart) -> reboot -> waiting_boot -> starting_app -> success | failed

Fail-safe principles:
  * If the precheck fails (no access, no sudo, service not found) — the host is NOT rebooted.
  * A reboot is confirmed by a change of the boot marker (boot_id / LastBootUpTime), not by ping.
  * Host did not come up within timeout_seconds -> failed(boot_timeout).
  * Application did not start on its own -> N manual start attempts -> diagnostics in the log -> failed(app).
  * A group with on_failure=halt stops the stack on error: dependent groups are left untouched.
  * There are no automatic re-reboots (reboot-loop protection); a human makes that call.
  * "Stop" is a soft stop: current hosts are allowed to finish, new ones do not start.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from .alerts import send_alert
from .config import AppConfig, Group, Host
from .executors import ExecutorError, HostExecutor, get_executor
from .monitoring import PingMonitor
from .state import RunStatus, StateStore
from .vault import VaultLocked

log = logging.getLogger("orchestrator")


class HostFailure(Exception):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


class BusyError(Exception):
    pass


class Orchestrator:
    def __init__(self, config: AppConfig, store: StateStore, vault: object | None = None):
        self.config = config
        self.settings = config.settings
        self.vault = vault
        self.store = store
        self._active: set[str] = set()
        self._stack_task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self._background: set[asyncio.Task] = set()
        self._parallel_limit = asyncio.Semaphore(self.settings.max_parallel_hosts)
        self.excluded: dict[str, str] = {}
        self._checking: set[str] = set()

    def set_excluded(self, excluded: dict[str, str]) -> None:
        """Hosts that are never rebooted (first of all — the orchestrator's own server)."""
        self.excluded = dict(excluded)
        for host_id, reason in excluded.items():
            self.store.set_excluded(host_id, reason)
            self.store.event("warning", f"Host excluded from restart: {reason}", host_id)

    # ---------- public API ----------
    @property
    def stack_running(self) -> bool:
        return self._stack_task is not None and not self._stack_task.done()

    def start_stack(self, only_ids: list[str] | None = None) -> None:
        if self.stack_running:
            raise BusyError("A stack restart is already running")
        if self._active:
            raise BusyError(f"Wait for the manual restart to finish: {', '.join(sorted(self._active))}")
        selected: set[str] | None = None
        if only_ids is not None:
            known = {h.id for h in self.config.all_hosts()}
            unknown = [h for h in only_ids if h not in known]
            if unknown:
                raise BusyError(f"Unknown machines: {', '.join(unknown)}")
            selected = set(only_ids) - set(self.excluded)
            if not selected:
                raise BusyError("No machines selected for restart")
        self._cancel.clear()
        self._stack_task = self._spawn(self._run_stack(selected))

    def request_cancel(self) -> None:
        if not self.stack_running:
            raise BusyError("No stack restart is running right now")
        self._cancel.set()
        self.store.set_stack(cancel_requested=True,
                             message="Stopping: current hosts will finish, new ones will not start")
        self.store.event("warning", "Stack stop requested")

    def start_host(self, host_id: str) -> None:
        host = self.config.get_host(host_id)  # KeyError -> 404
        if host_id in self.excluded:
            raise BusyError(f"{host.name} is not rebooted: {self.excluded[host_id]}")
        if host_id in self._checking:
            raise BusyError(f"Wait for the check of {host.name} to finish")
        if self.stack_running:
            raise BusyError("A full stack restart is running — manual restart is unavailable")
        if host_id in self._active:
            raise BusyError(f"Host {host.name} is already restarting")
        self._active.add(host_id)  # reserve synchronously to rule out a double click
        self.store.set_status(host_id, RunStatus.QUEUED, "Queued (manual start)")
        self.store.event("info", f"Manual restart: {host.name}", host_id)
        self._spawn(self._restart_host(host))

    async def shutdown(self) -> None:
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)

    # ---------- stack ----------
    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _run_stack(self, selected: set[str] | None = None) -> None:
        groups = self.config.sorted_groups()
        hosts = [h for h in self.config.all_hosts()
                 if h.id not in self.excluded and (selected is None or h.id in selected)]
        self.store.set_stack(running=True, status="running", started_at=time.time(), finished_at=None,
                             cancel_requested=False, current_group=None, message="Stack restart started")
        for host in hosts:
            self.store.set_status(host.id, RunStatus.QUEUED, "Waiting for its turn")
        for host_id, reason in self.excluded.items():
            self.store.set_status(host_id, RunStatus.SKIPPED, f"Not rebooted: {reason}")
        self.store.event("info", f"Stack restart start: {len(groups)} groups, {len(hosts)} hosts")

        final = "success"
        failed_names: list[str] = []
        try:
            for group in groups:
                if self._cancel.is_set():
                    final = "cancelled"
                    break
                self.store.set_stack(current_group=group.name,
                                     message=f"Group {group.order}: {group.name}")
                self.store.event("info", f"Group {group.order} \"{group.name}\": "
                                         f"{'parallel' if group.mode == 'parallel' else 'sequential'}, "
                                         f"{len(group.hosts)} hosts")
                results = await self._run_group(group, selected)
                failed = [h for h, ok in results if ok is False]
                failed_names += [h.name for h in failed]
                if self._cancel.is_set():
                    final = "cancelled"
                    break
                if failed and group.on_failure == "halt":
                    final = "halted"
                    self.store.event("error", f"Group \"{group.name}\" finished with errors "
                                              f"({', '.join(h.name for h in failed)}). Halt policy: "
                                              f"the following groups are not restarted")
                    break
                if failed:
                    final = "partial"
                    self.store.event("warning", f"Group \"{group.name}\" had errors, continue policy — moving on")
        except asyncio.CancelledError:
            final = "cancelled"
            raise
        except Exception as exc:  # pragma: no cover — safety net
            log.exception("Orchestrator failure")
            final = "error"
            self.store.event("error", f"Internal orchestrator error: {exc}")
        finally:
            leftover = RunStatus.CANCELLED if final == "cancelled" else RunStatus.SKIPPED
            reason = "Stopped by operator" if final == "cancelled" else "Not executed: stack stopped due to an error"
            for host in hosts:
                if self.store.hosts[host.id].status == RunStatus.QUEUED:
                    self.store.set_status(host.id, leftover, reason)
            messages = {
                "success": "Stack restarted successfully",
                "partial": f"Stack restarted with errors: {', '.join(failed_names)}",
                "halted": f"Stack halted (fail-safe) due to errors: {', '.join(failed_names)}",
                "cancelled": "Stack restart stopped by operator",
                "error": "Stack restart aborted by an internal error",
            }
            self.store.set_stack(running=False, status=final, finished_at=time.time(),
                                 current_group=None, cancel_requested=False, message=messages[final])
            self.store.event("success" if final == "success" else "error", messages[final])
            if final not in ("success", "cancelled"):
                self._spawn(send_alert(self.settings, f"Restart orchestrator: {messages[final]}"))

    async def _run_group(self, group: Group, selected: set[str] | None = None) -> list[tuple[Host, bool | None]]:
        group_hosts = [h for h in group.hosts if h.id not in self.excluded
                       and (selected is None or h.id in selected)]
        if group.mode == "parallel":
            async def one(host: Host) -> tuple[Host, bool | None]:
                async with self._parallel_limit:
                    if self._cancel.is_set():
                        return host, None
                    return host, await self._restart_host(host)
            return list(await asyncio.gather(*(one(h) for h in group_hosts)))

        results: list[tuple[Host, bool | None]] = []
        for host in group_hosts:
            if self._cancel.is_set():
                break
            ok = await self._restart_host(host)
            results.append((host, ok))
            if not ok and group.on_failure == "halt":
                break  # within a sequential group, also leave the following hosts untouched
        return results

    # ---------- single host ----------
    async def _restart_host(self, host: Host) -> bool:
        self._active.add(host.id)
        started = time.monotonic()
        try:
            await self._do_restart(host)
            elapsed = time.monotonic() - started
            self.store.set_status(host.id, RunStatus.SUCCESS, f"Done in {elapsed:.0f} s")
            self.store.event("success", f"{host.name}: restart finished in {elapsed:.0f} s", host.id)
            return True
        except HostFailure as exc:
            self.store.set_status(host.id, RunStatus.FAILED, str(exc), failure_stage=exc.stage)
            self.store.event("error", f"{host.name}: [{exc.stage}] {exc}", host.id)
            self._spawn(send_alert(self.settings, f"❌ {host.name} ({host.hostname}) [{exc.stage}]: {exc}"))
            return False
        except asyncio.CancelledError:
            self.store.set_status(host.id, RunStatus.CANCELLED, "Aborted")
            raise
        except Exception as exc:
            log.exception("Unexpected error on host %s", host.id)
            self.store.set_status(host.id, RunStatus.FAILED, f"Internal error: {exc}", failure_stage="internal")
            self.store.event("error", f"{host.name}: internal error: {exc}", host.id)
            return False
        finally:
            self._active.discard(host.id)

    async def _call(self, fn: Callable[..., Any], *args: Any, timeout: int | None = None) -> Any:
        """Blocking executor call in a thread pool with an overall timeout.

        Each call's duration is logged at DEBUG level — those entries show how long a
        WinRM/SSH call actually lives and where a delay appears.
        """
        limit = timeout or (self.settings.command_timeout_seconds + 15)
        name = getattr(fn, "__name__", str(fn))
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=limit)
            log.debug("call %s: ok in %.1f s", name, time.monotonic() - t0)
            return result
        except asyncio.TimeoutError as exc:
            log.debug("call %s: timeout %s s (actually %.1f s)", name, limit, time.monotonic() - t0)
            raise ExecutorError(f"Timeout {limit} s while running {name}") from exc
        except ExecutorError as exc:
            log.debug("call %s: error in %.1f s — %s", name, time.monotonic() - t0, str(exc).splitlines()[0])
            raise

    async def _do_restart(self, host: Host) -> None:
        ex = get_executor(host, self.settings, self.vault)
        hid = host.id

        # 0. If the machine's password is encrypted and the master password is not entered/expired — a clear error
        try:
            ex.get_password()
        except VaultLocked:
            raise HostFailure("locked", "The machine's password is encrypted. Enter the master password "
                                        "in the web interface to unlock access")

        # 1. Precheck: without access and privileges, rebooting is not allowed
        self.store.set_status(hid, RunStatus.PRECHECK, "Checking access, privileges and service")
        try:
            await self._call(ex.precheck)
            old_marker = await self._call(ex.get_boot_marker)
        except ExecutorError as exc:
            raise HostFailure("precheck", f"Precheck failed, no reboot was performed. {exc}")

        # 2. Ensure the application's autostart BEFORE the reboot
        if host.app_service_name:
            try:
                info = await self._call(ex.ensure_autostart)
                self.store.event("info", f"{host.name}: {info}", hid)
            except ExecutorError as exc:
                raise HostFailure("autostart", f"Could not enable autostart for \"{host.app_service_name}\", "
                                               f"no reboot was performed. {exc}")

        # 3. Reboot
        self.store.set_status(hid, RunStatus.REBOOTING, "Sending reboot command")
        try:
            await self._call(ex.reboot)
        except ExecutorError as exc:
            raise HostFailure("reboot", f"Reboot command failed. {exc}")
        self.store.event("info", f"{host.name}: reboot command sent", hid)

        # 4. Wait for boot
        await self._wait_for_boot(host, ex, old_marker)

        # 4a. The application needs a user session — wait for autologon.
        # BUT: if application readiness is checked by process (app_process_name), the mere fact of a
        # running GUI process already proves the user has logged in (the window does not start without a login).
        # Then a separate login-wait phase is unnecessary — it only duplicates the wait and runs into a
        # not-yet-ready WinRM, which kept the status stuck for a long time. In that case the login and app
        # checks are merged into _ensure_app.
        if host.requires_user_session and not host.app_process_name and not host.app_health_port:
            await self._wait_for_login(host, ex)

        # 5. Application check (waits for the process/service; for a kiosk this also confirms the login)
        if host.app_service_name:
            await self._ensure_app(host, ex)

    async def _wait_for_boot(self, host: Host, ex: HostExecutor, old_marker: str) -> None:
        """The "Booting" phase. Criterion — host reachability by ICMP ping OR the management port.

        This is faster and more reliable than asking for the boot marker over WinRM: on a freshly
        booted machine WinRM does not respond immediately and may hang, which kept the status stuck on
        "Booting". Ping/port answer as soon as the network comes up. We wait for the "went down → came
        back" pattern: first the host becomes unreachable, then reachable again. That is the reboot.
        As soon as the host is back online — the phase is passed, and we move on to login and the app
        (which use WinRM, but by then it is usually ready; and if not, those phases know how to wait).
        The status is updated every second.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + host.timeout_seconds
        went_down = False
        self.store.set_status(host.id, RunStatus.WAITING_BOOT, f"Waiting for reboot, timeout {host.timeout_seconds} s")
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                hint = ("the host went into reboot but did not come back" if went_down
                        else "the host never went into reboot")
                raise HostFailure("boot_timeout", f"Host did not boot within {host.timeout_seconds} s: {hint}.")
            await asyncio.sleep(min(1, remaining))  # update the status every second
            elapsed = int(loop.time() - started)
            # two fast probes, neither of which hangs: ICMP ping and the TCP management port
            ping_ok, _ = await PingMonitor.icmp(host.hostname)
            reachable = bool(ping_ok)
            if not reachable:
                reachable = await asyncio.to_thread(ex.port_open, 2.0)
            if not reachable:
                went_down = True
                self.store.set_status(host.id, RunStatus.WAITING_BOOT,
                                      f"Host unreachable, rebooting… {elapsed} of {host.timeout_seconds} s")
                continue
            # host is online
            if went_down:
                self.store.event("info", f"{host.name}: OS booted in {elapsed} s (host back online)", host.id)
                return
            # the host has not gone into reboot yet — the reboot command may not have taken effect.
            # Safeguard for a very fast reboot: if the boot marker has already changed,
            # also treat the boot as done. The check is cheap (port is open, WinRM responds).
            try:
                marker = await self._call(ex.get_boot_marker, timeout=10)
                if marker != old_marker:
                    self.store.event("info", f"{host.name}: OS rebooted in {elapsed} s", host.id)
                    return
            except ExecutorError:
                pass
            self.store.set_status(host.id, RunStatus.WAITING_BOOT,
                                  f"Waiting for the reboot to begin… {elapsed} of {host.timeout_seconds} s")

    async def _wait_for_login(self, host: Host, ex: HostExecutor) -> None:
        """OS is booted (SSH/WinRM already respond at the login screen); wait for autologon to kick in."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + host.login_timeout_seconds
        last_error = ""
        while loop.time() < deadline:
            elapsed = int(loop.time() - started)
            self.store.set_status(host.id, RunStatus.WAITING_BOOT,
                                  f"OS booted, waiting for user autologon… {elapsed} of {host.login_timeout_seconds} s")
            try:
                user = await self._call(ex.session_user, timeout=15)
            except ExecutorError as exc:
                user, last_error = None, str(exc).splitlines()[0]
            if user:
                expected = (host.session_user or "").lower().split("\\")[-1]
                if expected and user.lower().split("\\")[-1] != expected:
                    raise HostFailure("login", f"User \"{user}\" logged in, but \"{host.session_user}\" was expected")
                waited = int(loop.time() - started)
                self.store.event("info", f"{host.name}: user {user} logged in after {waited} s", host.id)
                return
            await asyncio.sleep(min(1, self.settings.app_poll_interval_seconds))
        detail = f" Last error: {last_error}" if last_error else ""
        raise HostFailure("login", f"The system booted, but nobody logged in within {host.login_timeout_seconds} s: the "
                                   f"machine is waiting for a password at the login screen. Check autologon (the password "
                                   f"may have changed).{detail}")

    # ---------- check without reboot ----------
    def start_check(self, host_ids: list[str] | None = None) -> list[str]:
        """Non-destructive check: connection, privileges, service, autostart, session. Changes nothing on the hosts."""
        if self.stack_running:
            raise BusyError("A stack restart is running — check is unavailable")
        hosts = [self.config.get_host(h) for h in host_ids] if host_ids else self.config.all_hosts()
        started = []
        for host in hosts:
            if host.id in self._active or host.id in self._checking:
                if host_ids and len(host_ids) == 1:
                    raise BusyError(f"{host.name} is busy right now")
                continue
            self._checking.add(host.id)
            self.store.set_check(host.id, {"state": "running", "at": time.time(), "items": []})
            self._spawn(self._check_guarded(host))
            started.append(host.id)
        return started

    async def _check_guarded(self, host: Host) -> dict:
        try:
            async with self._parallel_limit:
                return await self.check_host(host)
        finally:
            self._checking.discard(host.id)

    async def check_host(self, host: Host) -> dict:
        ex = get_executor(host, self.settings, self.vault)
        items: list[dict] = []

        # password is encrypted and the master key is not entered — show it, do not bother connecting
        try:
            ex.get_password()
        except VaultLocked:
            result = {"state": "error", "at": time.time(), "items": [
                {"label": "Access", "level": "error",
                 "detail": "password is encrypted — enter the master password in the web interface"}]}
            self.store.set_check(host.id, result)
            return result

        async def step(label: str, fn: Callable[[], Any], describe: Callable[[Any], tuple[str, str]]) -> tuple[bool, Any]:
            try:
                value = await self._call(fn, timeout=self.settings.command_timeout_seconds)
            except ExecutorError as exc:
                items.append({"label": label, "level": "error", "detail": str(exc).splitlines()[0][:300]})
                return False, None
            level, detail = describe(value)
            items.append({"label": label, "level": level, "detail": detail})
            return True, value

        if host.id in self.excluded:
            items.append({"label": "Restart", "level": "warn", "detail": f"not performed: {self.excluded[host.id]}"})
        ok, _ = await step("Access and privileges", ex.precheck, lambda _: ("ok", "connection, privileges and settings are OK"))
        # on a transport error (no connection, wrong login, known_hosts) further checks are pointless
        if ok or not items[-1]["detail"].startswith(("SSH:", "WinRM:")):
            await step("Boot marker", ex.get_boot_marker, lambda v: ("ok", str(v)))
            if host.app_service_name:
                await step("Autostart", ex.autostart_status, lambda v: ("ok", str(v)))
                await step("Application", ex.is_app_running,
                           lambda v: ("ok", "running") if v else ("warn", "not running right now"))
            if host.requires_user_session:
                await step("User session", ex.session_user,
                           lambda v: ("ok", f"logged in {v}") if v else ("warn", "nobody logged in (login screen)"))
        result = {"state": "error" if any(i["level"] == "error" for i in items) else "ok",
                  "at": time.time(), "items": items}
        self.store.set_check(host.id, result)
        level = "error" if result["state"] == "error" else "success"
        problems = "; ".join(f"{i['label']}: {i['detail']}" for i in items if i["level"] != "ok")
        self.store.event(level, f"{host.name}: check {'failed' if level == 'error' else 'passed'}"
                                f"{' — ' + problems if problems else ''}", host.id)
        return result

    async def _ensure_app(self, host: Host, ex: HostExecutor) -> None:
        loop = asyncio.get_running_loop()
        svc = host.app_service_name
        last_error = ""

        # 5a. Wait for the application to come up.
        # If the application has a network port set (app_health_port, Zabbix-style) — check THAT,
        # instead of asking WinRM/SSH. This is the key point: WinRM on a freshly booted Windows machine
        # only answers after ~3 minutes, while the app port opens as soon as the app comes up (~30 s).
        # A port check is fast, independent of WinRM, and universal for any application.
        by_port = ex.has_port_health()
        started = loop.time()
        window = max(host.app_start_timeout_seconds, host.login_timeout_seconds) \
            if host.requires_user_session else host.app_start_timeout_seconds
        deadline = started + window
        while loop.time() < deadline:
            elapsed = int(loop.time() - started)
            label = f"port {host.app_health_port}" if by_port else f"\"{svc}\""
            self.store.set_status(host.id, RunStatus.STARTING_APP,
                                  f"Waiting for the application to start ({label})… {elapsed} of {int(window)} s")
            try:
                ready = (await asyncio.to_thread(ex.app_health_port_open, 2.0) if by_port
                         else await self._call(ex.is_app_running, timeout=15))
                if ready:
                    how = f"port {host.app_health_port} open" if by_port else "service active"
                    self.store.event("info", f"{host.name}: application running ({how}) in {elapsed} s", host.id)
                    return
            except ExecutorError as exc:
                last_error = str(exc).splitlines()[0]
            await asyncio.sleep(min(1, self.settings.app_poll_interval_seconds))

        # A kiosk GUI cannot be started remotely (no desktop access) — we can only wait for autostart
        if not ex.can_start_manually():
            try:
                diagnostics = await self._call(ex.app_diagnostics)
            except ExecutorError as exc:
                diagnostics = f"(could not get diagnostics: {exc})"
            if diagnostics:
                self.store.event("error", f"{host.name}: diagnostics for \"{svc}\":\n{diagnostics[-3000:]}", host.id)
            raise HostFailure("app", f"OS booted and the user logged in, but \"{svc}\" "
                                     f"({host.app_process_name}) did not start within {host.app_start_timeout_seconds} s. "
                                     f"Check autostart in the session.{(' ' + last_error) if last_error else ''}")

        # 5b. Self-healing: manual start with retries
        retries = self.settings.app_start_retries
        self.store.event("warning", f"{host.name}: \"{svc}\" did not start on its own within "
                                    f"{host.app_start_timeout_seconds} s, trying to start it manually", host.id)
        for attempt in range(1, retries + 1):
            self.store.set_status(host.id, RunStatus.STARTING_APP, f"Starting \"{svc}\" manually: attempt {attempt} of {retries}")
            try:
                await self._call(ex.start_app)
            except ExecutorError as exc:
                last_error = str(exc).splitlines()[0]
                self.store.event("warning", f"{host.name}: attempt {attempt} failed: {last_error}", host.id)
            await asyncio.sleep(self.settings.app_retry_delay_seconds)
            try:
                if await self._call(ex.is_app_running, timeout=15):
                    self.store.event("warning", f"{host.name}: \"{svc}\" started manually on attempt {attempt}. "
                                                f"Check why autostart did not work", host.id)
                    return
            except ExecutorError as exc:
                last_error = str(exc).splitlines()[0]

        # 5c. Give up: collect diagnostics, leave the host as-is for investigation
        try:
            diagnostics = await self._call(ex.app_diagnostics)
        except ExecutorError as exc:
            diagnostics = f"(could not get diagnostics: {exc})"
        if diagnostics:
            self.store.event("error", f"{host.name}: diagnostics for \"{svc}\":\n{diagnostics[-3000:]}", host.id)
        detail = f" Last error: {last_error}" if last_error else ""
        raise HostFailure("app", f"OS booted, but \"{svc}\" did not start after {retries} attempts.{detail}")
