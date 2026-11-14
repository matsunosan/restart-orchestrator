"""Windows over WinRM (pywinrm): reboot, Windows Services and Task Scheduler.

Requirements: WinRM enabled (deploy/windows/setup-winrm.ps1) and an account
with local administrator rights.
"""
from __future__ import annotations

import logging
import socket

import requests
import winrm
from winrm.exceptions import InvalidCredentialsError, WinRMError, WinRMTransportError

from .base import ExecutorError, HostExecutor

log = logging.getLogger(__name__)

PS_PREFIX = (
    "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'\n"
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
    "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
)


def ps_quote(value: str) -> str:
    """A single-quoted PowerShell string (no interpolation)."""
    return "'" + value.replace("'", "''") + "'"


class WindowsExecutor(HostExecutor):
    def _session(self, timeout: int | None = None) -> winrm.Session:
        password = self.get_password()
        if not password:
            raise ExecutorError("WinRM requires a password: set credentials.password_env (or password)")
        scheme = "https" if self.host.winrm_https else "http"
        timeout = timeout or self.settings.command_timeout_seconds
        return winrm.Session(
            f"{scheme}://{self.host.hostname}:{self.host.port}/wsman",
            auth=(self.host.user, password),
            transport=self.host.winrm_transport,
            server_cert_validation="validate" if self.host.winrm_verify_ssl else "ignore",
            operation_timeout_sec=timeout,
            read_timeout_sec=timeout + 10,
        )

    def _port_open(self, connect_timeout: float = 3.0) -> bool:
        """Fast check of whether the WinRM port is open. While the machine is rebooting the port is closed,
        and this is found out in seconds, not via a long TCP timeout inside pywinrm."""
        try:
            with socket.create_connection((self.host.hostname, self.host.port), timeout=connect_timeout):
                return True
        except OSError:
            return False

    def _ps(self, script: str, check: bool = True, timeout: int | None = None) -> tuple[int, str, str]:
        # while the port is closed (machine rebooting), do not hand control to pywinrm:
        # its TCP connect to a dead host can hang for tens of seconds
        if not self._port_open():
            raise ExecutorError(f"WinRM: port {self.host.hostname}:{self.host.port} not responding (machine unreachable)")
        try:
            result = self._session(timeout).run_ps(PS_PREFIX + script)
        except InvalidCredentialsError as exc:
            raise ExecutorError(f"WinRM: invalid credentials ({exc})") from exc
        except (WinRMError, WinRMTransportError, requests.exceptions.RequestException, OSError) as exc:
            raise ExecutorError(f"WinRM: no connection to {self.host.hostname}:{self.host.port} ({exc})") from exc
        out = result.std_out.decode("utf-8", errors="replace").strip()
        err = result.std_err.decode("utf-8", errors="replace").strip()
        if check and result.status_code != 0:
            raise ExecutorError(f"PowerShell returned code {result.status_code}: {err or out}")
        return result.status_code, out, err

    @property
    def _is_task(self) -> bool:
        return self.host.app_service_type == "windows_task"

    @property
    def _name(self) -> str:
        return ps_quote(self.service or "")

    # ---------- interface ----------
    def precheck(self) -> None:
        rc, out, _ = self._ps(
            "$p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())\n"
            "if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Write-Output 'NOT_ADMIN'; exit 3 }\n"
            "Write-Output 'OK'",
            check=False,
        )
        if rc != 0 or "NOT_ADMIN" in out:
            raise ExecutorError(f"Account {self.host.user} is not an administrator on the host")
        if self.service:
            cmd = f"Get-ScheduledTask -TaskName {self._name} | Out-Null" if self._is_task \
                else f"Get-Service -Name {self._name} | Out-Null"
            rc, _, err = self._ps(cmd, check=False)
            kind = "Scheduled task" if self._is_task else "Service"
            if rc != 0:
                raise ExecutorError(f"{kind} \"{self.service}\" not found: {err}")
        if self.host.requires_user_session:
            _, out, _ = self._ps(
                "$w = Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon'\n"
                "Write-Output \"$($w.AutoAdminLogon)|$($w.DefaultDomainName)|$($w.DefaultUserName)\""
            )
            enabled, domain, user = (out.split("|") + ["", "", ""])[:3]
            if enabled.strip() != "1":
                raise ExecutorError("Windows autologon is not configured: after reboot the machine will wait for a "
                                    "password at the login screen. Configure it with the Sysinternals Autologon tool.")
            expected = (self.host.session_user or "").lower()
            if expected and expected.split("\\")[-1] != user.strip().lower():
                raise ExecutorError(f"Autologon is set for \"{domain}\\{user}\", "
                                    f"but the config has session_user=\"{self.host.session_user}\"")

    def get_boot_marker(self) -> str:
        # short timeout: while the machine is rebooting, the attempt must fail fast,
        # so the wait loop frequently checks whether it has come back
        _, out, _ = self._ps(
            "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')",
            timeout=10,
        )
        return out

    def reboot(self) -> None:
        # shutdown.exe returns control immediately, the reboot begins in 5 seconds
        self._ps('shutdown.exe /r /t 5 /f /d p:0:0 /c "Remote restart by RestartOrchestrator"; exit $LASTEXITCODE')

    def ensure_autostart(self) -> str:
        if self._is_task:
            _, out, _ = self._ps(
                f"Enable-ScheduledTask -TaskName {self._name} | Out-Null\n"
                f"$t = Get-ScheduledTask -TaskName {self._name}\n"
                f"$boot = @($t.Triggers | Where-Object {{ $_.CimClass.CimClassName -eq '{self._trigger_class}' }}).Count\n"
                "Write-Output \"$($t.State)|$boot\""
            )
            state, _, boot = out.partition("|")
            if boot.strip() in ("", "0"):
                raise ExecutorError(f"Task \"{self.service}\" has no \"{self._trigger_name}\" trigger")
            return f"Task Scheduler: {self.service} enabled, state={state}, \"{self._trigger_name}\" triggers={boot}"
        _, out, _ = self._ps(
            f"Set-Service -Name {self._name} -StartupType Automatic\n"
            f"(Get-Service -Name {self._name}).StartType.ToString()"
        )
        if out != "Automatic":
            raise ExecutorError(f"Service startup type did not become Automatic (currently: {out})")
        return f"Windows Service: {self.service} StartupType=Automatic"

    @property
    def _trigger_class(self) -> str:
        # an app in a user session needs an "At log on" trigger, a service-task — "At startup"
        return "MSFT_TaskLogonTrigger" if self.host.requires_user_session else "MSFT_TaskBootTrigger"

    @property
    def _trigger_name(self) -> str:
        return "At log on" if self.host.requires_user_session else "At startup"

    def autostart_status(self) -> str:
        if self._is_task:
            _, out, _ = self._ps(
                f"$t = Get-ScheduledTask -TaskName {self._name}\n"
                f"$n = @($t.Triggers | Where-Object {{ $_.CimClass.CimClassName -eq '{self._trigger_class}' }}).Count\n"
                f"Write-Output \"state=$($t.State), '{self._trigger_name}' triggers=$n\""
            )
            return out
        _, out, _ = self._ps(f"\"StartType=$((Get-Service -Name {self._name}).StartType)\"")
        return out

    def session_user(self) -> str | None:
        # short timeout: called in the login-wait loop; a stuck attempt must not
        # block for a minute (otherwise the status "hangs" even though the user has logged in)
        _, out, _ = self._ps("(Get-CimInstance Win32_ComputerSystem).UserName", timeout=10)
        return out or None

    def is_app_running(self) -> bool:
        # short timeout: called in the application start-wait loop
        proc = self.host.app_process_name
        if proc:
            # the exe is started by a task/batch that then exited: check the process itself, not the task
            base = proc[:-4] if proc.lower().endswith(".exe") else proc
            _, out, _ = self._ps(f"@(Get-Process -Name {ps_quote(base)} -ErrorAction SilentlyContinue).Count",
                                 timeout=10)
            return out.strip() not in ("", "0")
        if self._is_task:
            _, out, _ = self._ps(f"(Get-ScheduledTask -TaskName {self._name}).State.ToString()", timeout=10)
        else:
            _, out, _ = self._ps(f"(Get-Service -Name {self._name}).Status.ToString()", timeout=10)
        return out == "Running"

    def start_app(self) -> None:
        if self._is_task:
            self._ps(f"Start-ScheduledTask -TaskName {self._name}")
        else:
            self._ps(f"Start-Service -Name {self._name}")

    def app_diagnostics(self) -> str:
        if self.host.app_process_name:
            proc = self.host.app_process_name
            base = proc[:-4] if proc.lower().endswith(".exe") else proc
            _, out, err = self._ps(
                f"Get-ScheduledTaskInfo -TaskName {self._name} | "
                "Format-List LastRunTime,LastTaskResult | Out-String\n"
                f"$n = @(Get-Process -Name {ps_quote(base)} -ErrorAction SilentlyContinue).Count\n"
                f"Write-Output \"Process {proc}: $n running\"",
                check=False)
            return out or err
        if self._is_task:
            script = (f"Get-ScheduledTaskInfo -TaskName {self._name} | "
                      "Format-List LastRunTime,LastTaskResult,NextRunTime | Out-String")
        else:
            script = (
                f"$s = Get-Service -Name {self._name}\n"
                "Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Service Control Manager'; "
                "StartTime=(Get-Date).AddMinutes(-30)} -MaxEvents 100 -ErrorAction SilentlyContinue | "
                "Where-Object { $_.Message -like \"*$($s.DisplayName)*\" } | Select-Object -First 10 | "
                "ForEach-Object { \"$($_.TimeCreated) [$($_.Id)] $($_.Message)\" }"
            )
        _, out, err = self._ps(script, check=False)
        return out or err
