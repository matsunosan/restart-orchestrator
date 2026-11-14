"""Linux (systemd) and macOS (launchd) over SSH (paramiko).

sudo works in two modes:
  * passwordless (NOPASSWD for specific commands, recommended for production): sudo -n;
  * with a password (credentials.sudo_password / sudo_password_env / secrets.json): the password
    is passed to sudo -S via stdin, so it never appears on the command line or in the process list.
"""
from __future__ import annotations

import logging
import os
import shlex

import paramiko

from .base import ExecutorError, HostExecutor

log = logging.getLogger(__name__)

SUDO_BAD_PASSWORD = ("incorrect password", "Sorry, try again")


class SSHExecutor(HostExecutor):
    # ---------- transport ----------
    def _connect(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self.settings.ssh_known_hosts_file:
            known = os.path.expanduser(self.settings.ssh_known_hosts_file)
            if os.path.exists(known):
                client.load_host_keys(known)
        if self.settings.ssh_strict_host_key_checking:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        creds = self.host.credentials
        kwargs: dict = dict(
            hostname=self.host.hostname,
            port=self.host.port,
            username=self.host.user,
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
        )
        password = self.get_password()
        if creds.ssh_key:
            kwargs.update(
                key_filename=os.path.expanduser(creds.ssh_key),
                passphrase=creds.get_key_passphrase(),
                look_for_keys=False,
                allow_agent=False,
            )
        if password:
            kwargs["password"] = password
            if not creds.ssh_key:
                kwargs.update(look_for_keys=False, allow_agent=False)

        try:
            client.connect(**kwargs)
        except paramiko.AuthenticationException as exc:
            client.close()
            raise ExecutorError(f"SSH: authentication failed for user {self.host.user} ({exc})") from exc
        except paramiko.BadHostKeyException as exc:
            client.close()
            raise ExecutorError(f"SSH: host key does not match known_hosts ({exc})") from exc
        except paramiko.SSHException as exc:
            client.close()
            if "not found in known_hosts" in str(exc):
                raise ExecutorError(f"SSH: host is not in known_hosts, run ssh-keyscan -H {self.host.hostname} "
                                    f">> ~/.ssh/known_hosts") from exc
            raise ExecutorError(f"SSH: protocol error ({exc})") from exc
        except OSError as exc:
            client.close()
            raise ExecutorError(f"SSH: no connection to {self.host.hostname}:{self.host.port} ({exc})") from exc
        return client

    def _run(self, cmd: str, timeout: int | None = None, check: bool = True,
             stdin_data: str | None = None) -> tuple[int, str, str]:
        timeout = timeout or self.settings.command_timeout_seconds
        client = self._connect()
        try:
            stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
            if stdin_data is not None:
                stdin.write(stdin_data)
                stdin.flush()
                stdin.channel.shutdown_write()
            out = stdout.read().decode(errors="replace").strip()
            err = stderr.read().decode(errors="replace").strip()
            rc = stdout.channel.recv_exit_status()
        except (TimeoutError, paramiko.SSHException, OSError) as exc:
            raise ExecutorError(f"SSH: command failed ({exc}): {cmd}") from exc
        finally:
            client.close()
        log.debug("[%s] $ %s -> rc=%s", self.host.id, cmd, rc)
        if check and rc != 0:
            raise ExecutorError(f"Command returned code {rc}: {cmd}\n{err or out}")
        return rc, out, err

    # ---------- sudo ----------
    @property
    def _sudo_password(self) -> str | None:
        return self.get_sudo_password()

    def _sudo(self, cmd: str, check: bool = True, timeout: int | None = None) -> tuple[int, str, str]:
        password = self._sudo_password
        if not password:
            return self._run(f"sudo -n {cmd}", check=check, timeout=timeout)
        rc, out, err = self._run(f"sudo -S -p '' {cmd}", check=False, timeout=timeout, stdin_data=password + "\n")
        if rc != 0 and any(marker in err for marker in SUDO_BAD_PASSWORD):
            raise ExecutorError(f"sudo: password rejected for user {self.host.user}")
        if check and rc != 0:
            raise ExecutorError(f"sudo: command returned code {rc}: {cmd}\n{err or out}")
        return rc, out, err

    def _check_sudo(self) -> None:
        if self._sudo_password:
            rc, _, err = self._run("sudo -S -p '' -v", check=False, stdin_data=self._sudo_password + "\n")
            if rc != 0:
                raise ExecutorError(f"sudo: password rejected or {self.host.user} has no sudo rights ({err[:200]})")
            return
        rc, out, err = self._run("sudo -n -l", check=False)
        if rc != 0 or "NOPASSWD" not in out:
            raise ExecutorError(
                f"User {self.host.user} has no passwordless sudo (NOPASSWD). Either configure sudoers "
                f"following the example in deploy/, or set sudo_password in secrets.json. {err}"
            )

    def _background(self, cmd: str) -> None:
        """Start via nohup with a delay so the SSH session can close cleanly first."""
        inner = shlex.quote(f"sleep 2; {cmd}")
        self._run(f"nohup sh -c {inner} >/dev/null 2>&1 &")


class LinuxExecutor(SSHExecutor):
    def _svc(self) -> str:
        return shlex.quote(self.service or "")

    @property
    def _is_desktop(self) -> bool:
        return self.host.app_service_type == "desktop_autostart"

    def _proc_count(self) -> int:
        proc = self.host.app_process_name or ""
        rc, out, _ = self._run(f"pgrep -c -f {shlex.quote(proc)}", check=False, timeout=20)
        return int(out) if out.isdigit() else 0

    def precheck(self) -> None:
        self._check_sudo()
        if self._is_desktop:
            # kiosk: there is no service, check the autostart file in the graphical user's home folder
            user = self.host.session_user or self.host.user
            path = f"/home/{user}/.config/autostart/{self.service}.desktop"
            rc, _, _ = self._run(f"test -f {shlex.quote(path)}", check=False)
            if rc != 0:
                raise ExecutorError(
                    f"Autostart file {path} not found. On the host, perform Step 2 of the Linux "
                    f"kiosk setup guide (create ~/.config/autostart/{self.service}.desktop)")
            return
        if self.service:
            rc, _, _ = self._run(f"systemctl cat {self._svc()} >/dev/null 2>&1", check=False)
            if rc != 0:
                raise ExecutorError(f"systemd unit \"{self.service}\" not found on the host")

    def get_boot_marker(self) -> str:
        _, out, _ = self._run("cat /proc/sys/kernel/random/boot_id", timeout=20)
        return out

    def reboot(self) -> None:
        # systemd-run schedules the reboot via a timer in 3 s and returns control immediately:
        # the SSH session closes normally, and sudo -S gets the password via stdin.
        self._sudo("systemd-run --on-active=3 --timer-property=AccuracySec=100ms systemctl reboot")

    def ensure_autostart(self) -> str:
        if self._is_desktop:
            # autostart is provided by the .desktop file in the user session; its presence was verified in precheck
            return f"desktop autostart: {self.host.app_process_name} starts on graphical login"
        self._sudo(f"systemctl enable {self._svc()}")
        state = self.autostart_status()
        if state not in {"enabled", "enabled-runtime", "static", "alias", "indirect", "generated"}:
            raise ExecutorError(f"Autostart did not enable, systemctl is-enabled = {state!r}")
        return f"systemd: {self.service} is-enabled={state}"

    def autostart_status(self) -> str:
        if self._is_desktop:
            return f"autostart file in place, process {self.host.app_process_name}"
        _, state, _ = self._run(f"systemctl is-enabled {self._svc()}", check=False)
        return state or "unknown"

    def is_app_running(self) -> bool:
        if self._is_desktop:
            return self._proc_count() > 0
        rc, _, _ = self._run(f"systemctl is-active --quiet {self._svc()}", check=False, timeout=20)
        return rc == 0

    def can_start_manually(self) -> bool:
        return not self._is_desktop

    def start_app(self) -> None:
        if self._is_desktop:
            raise ExecutorError("A kiosk application starts on graphical login by itself; "
                                "a manual start over SSH is impossible (no desktop access)")
        self._sudo(f"systemctl start {self._svc()}")

    def session_user(self) -> str | None:
        # who is logged into the graphical session
        _, out, _ = self._run("who | awk '/\\(:[0-9]/{print $1; exit}'", check=False, timeout=20)
        if not out:
            _, out, _ = self._run("loginctl list-sessions --no-legend 2>/dev/null | awk '$0 ~ /seat/{print $3; exit}'",
                                  check=False, timeout=20)
        return out or None

    def app_diagnostics(self) -> str:
        if self._is_desktop:
            _, who, _ = self._run("who", check=False)
            _, procs, _ = self._run(f"pgrep -a -f {shlex.quote(self.host.app_process_name or '')}", check=False)
            return f"Logged-in sessions:\n{who}\n--- application processes ---\n{procs or 'none found'}"
        _, status, _ = self._run(f"systemctl status {self._svc()} --no-pager -n 0", check=False)
        _, journal, _ = self._sudo(f"journalctl -u {self._svc()} -n 30 --no-pager", check=False)
        return f"{status}\n--- journal ---\n{journal}"


class MacExecutor(SSHExecutor):
    """launchd: LaunchDaemon (system, no user login) or LaunchAgent (gui/<uid>, login required)."""

    @property
    def _is_agent(self) -> bool:
        return self.host.app_service_type == "launchagent"

    @property
    def _plist(self) -> str:
        folder = "LaunchAgents" if self._is_agent else "LaunchDaemons"
        return f"/Library/{folder}/{self.service}.plist"

    def _domain(self) -> str:
        if not self._is_agent:
            return "system"
        _, uid, _ = self._run(f"id -u {shlex.quote(self.host.session_user or '')}")
        return f"gui/{uid}"

    def _loaded(self, domain: str) -> tuple[bool, str]:
        target = shlex.quote(f"{domain}/{self.service}")
        rc, out, _ = self._sudo(f"launchctl print {target}", check=False, timeout=20)
        return rc == 0, out

    def _run_at_load(self) -> str:
        _, value, _ = self._run(f"/usr/libexec/PlistBuddy -c 'Print :RunAtLoad' {shlex.quote(self._plist)}", check=False)
        return value.strip().lower()

    def precheck(self) -> None:
        self._check_sudo()
        _, fv, _ = self._run("fdesetup status", check=False)
        if "FileVault is On" in fv:
            raise ExecutorError(
                "FileVault is on: after reboot the Mac stops at the disk-unlock screen, "
                "where there is no network or SSH yet, and will not come back automatically. Disable FileVault on this "
                "machine or exclude it from restarts."
            )
        if self.service:
            rc, _, _ = self._run(f"test -f {shlex.quote(self._plist)}", check=False)
            if rc != 0:
                raise ExecutorError(f"{self._plist} not found")
        if self.host.requires_user_session:
            rc, user, _ = self._run("defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser", check=False)
            if rc != 0 or not user:
                raise ExecutorError("Automatic login is not configured: after reboot the Mac will wait for a password "
                                    "at the login screen. Enable it in Settings → Users & Groups.")
            if self.host.session_user and user != self.host.session_user:
                raise ExecutorError(f"Autologon is set for \"{user}\", "
                                    f"but the config has session_user=\"{self.host.session_user}\"")

    def get_boot_marker(self) -> str:
        _, out, _ = self._run("sysctl -n kern.boottime", timeout=20)
        return out

    def reboot(self) -> None:
        if self._sudo_password:
            # sudo -S reads the password from stdin, then a root shell starts the reboot in the background
            background = f"nohup sh -c {shlex.quote('sleep 2; /sbin/shutdown -r now')} >/dev/null 2>&1 &"
            self._sudo(f"sh -c {shlex.quote(background)}")
        else:
            self._background("sudo -n /sbin/shutdown -r now")

    def ensure_autostart(self) -> str:
        if self._run_at_load() != "true":
            raise ExecutorError(f"{self._plist} does not set RunAtLoad=true — the app will not start after boot")
        if self._is_agent:
            # the gui/<uid> domain exists only while the user is logged in; an agent from /Library/LaunchAgents
            # loads itself on autologon, so an enable error here is not treated as fatal
            try:
                self._sudo(f"launchctl enable {shlex.quote(self._domain() + '/' + self.service)}", check=False)
            except ExecutorError:
                pass
            return f"launchd agent: {self.service}, RunAtLoad=true (starts after autologon)"
        domain = self._domain()
        self._sudo(f"launchctl enable {shlex.quote(domain + '/' + self.service)}")
        if not self._loaded(domain)[0]:
            self._sudo(f"launchctl bootstrap {domain} {shlex.quote(self._plist)}")
        return f"launchd daemon: {self.service} enabled, RunAtLoad=true"

    def autostart_status(self) -> str:
        loaded = self._loaded(self._domain())[0] if not self._is_agent or self.session_user() else False
        return f"RunAtLoad={self._run_at_load() or 'no'}, loaded={'yes' if loaded else 'no'}"

    def is_app_running(self) -> bool:
        loaded, out = self._loaded(self._domain())
        return loaded and "state = running" in out

    def start_app(self) -> None:
        domain = self._domain()
        if not self._loaded(domain)[0]:
            self._sudo(f"launchctl bootstrap {domain} {shlex.quote(self._plist)}")
        self._sudo(f"launchctl kickstart {shlex.quote(domain + '/' + self.service)}")

    def app_diagnostics(self) -> str:
        _, out = self._loaded(self._domain())
        return "\n".join(out.splitlines()[:40])

    def session_user(self) -> str | None:
        _, user, _ = self._run("stat -f %Su /dev/console", timeout=20)
        return None if user in ("", "root", "loginwindow") else user
