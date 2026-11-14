"""Web-interface login sessions (a cookie instead of the browser Basic Auth).

Login via the app page: username+password -> verification -> a session token in a cookie.
Tokens live in memory and expire on an inactivity timeout. When the program restarts, all
sessions are reset (you need to log in again) — that is normal.
"""
from __future__ import annotations

import os
import secrets
import threading
import time


class SessionStore:
    def __init__(self, ttl_seconds: int = 43200):  # 12 hours by default
        self.ttl = ttl_seconds
        self._sessions: dict[str, float] = {}  # token -> last_seen
        self._lock = threading.Lock()

    @staticmethod
    def auth_disabled() -> bool:
        return os.environ.get("AUTH_DISABLED") == "1"

    @staticmethod
    def accounts() -> list[tuple[str, str, str]]:
        """List of (username, password, role). Roles: admin (everything) and se (view/restart)."""
        out = []
        admin_u, admin_p = os.environ.get("ADMIN_USER"), os.environ.get("ADMIN_PASSWORD")
        if admin_u and admin_p:
            out.append((admin_u, admin_p, "admin"))
        se_u, se_p = os.environ.get("SE_USER"), os.environ.get("SE_PASSWORD")
        if se_u and se_p:
            out.append((se_u, se_p, "se"))
        return out

    @staticmethod
    def credentials() -> tuple[str | None, str | None]:
        # compatibility: whether there is at least one account
        accs = SessionStore.accounts()
        return (accs[0][0], accs[0][1]) if accs else (None, None)

    def check_login(self, user: str, password: str) -> str | None:
        """Return the role on correct credentials, otherwise None. Timing-safe comparison."""
        role = None
        for u, p, r in self.accounts():
            if (secrets.compare_digest(user.encode(), u.encode())
                    and secrets.compare_digest(password.encode(), p.encode())):
                role = r
        return role

    def create(self, role: str = "admin") -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = (time.time(), role)
        return token

    def _entry(self, token: str | None):
        if not token:
            return None
        with self._lock:
            e = self._sessions.get(token)
            if e is None:
                return None
            last, role = e
            now = time.time()
            if now - last > self.ttl:
                del self._sessions[token]
                return None
            self._sessions[token] = (now, role)  # extend
            return role

    def valid(self, token: str | None) -> bool:
        return self._entry(token) is not None

    def role(self, token: str | None) -> str | None:
        return self._entry(token)

    def destroy(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)
