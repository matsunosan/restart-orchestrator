"""Program entry point (for running and for building the exe).

Starts the web server. All settings come from the settings.ini file next to the program
(or from environment variables — they take priority). If settings.ini is missing, an example
one is created on first run.

Start the server:
    restart-orchestrator.exe            (or python run.py)

Utility commands (work from the exe, no Python needed):
    restart-orchestrator.exe gencert --host 10.0.0.10        — create an HTTPS certificate
    restart-orchestrator.exe setpass enable                  — set the master password
    restart-orchestrator.exe setpass set media-pc-03         — set a machine password
    restart-orchestrator.exe setpass rekey                   — change the master password (knowing the old one)
    restart-orchestrator.exe setpass status                  — encryption status
    restart-orchestrator.exe check                           — check access to the machines
"""
from __future__ import annotations

import configparser
import os
import sys
from pathlib import Path

# the folder where the program lives (next to the exe or run.py)
if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).parent
else:
    BASE = Path(__file__).resolve().parent

SETTINGS_FILE = BASE / "settings.ini"

DEFAULT_SETTINGS = """\
; Restart Orchestrator startup settings.
; Edit to taste and restart the program.

[server]
; listen address: 0.0.0.0 — all networks, 127.0.0.1 — local only
host = 0.0.0.0
port = 8443

; path to config.json (default: next to the program)
config = config.json

[auth]
; ADMIN — can do everything, including add/edit/delete machines
admin_user = admin
admin_password = CHANGE_ME_ADMIN
; SE (engineer) — can view, check and restart, but NOT edit the machine list
se_user = SE
se_password = CHANGE_ME_SE

; disable login entirely (ONLY for local testing, never set 1 in production)
auth_disabled = 0

[https]
; enable HTTPS (recommended when accessed over the network)
enabled = 1
; certificate paths (created by the gencert command)
certfile = certs/server.crt
keyfile = certs/server.key
"""


def ensure_settings() -> configparser.ConfigParser:
    if not SETTINGS_FILE.exists():
        SETTINGS_FILE.write_text(DEFAULT_SETTINGS, encoding="utf-8")
        print(f"Settings file created: {SETTINGS_FILE}")
        print("Edit it (login/password, certificate) and run again.")
    cfg = configparser.ConfigParser()
    cfg.read(SETTINGS_FILE, encoding="utf-8")
    return cfg


def _get(cfg, section, key, default=""):
    try:
        return cfg.get(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return default


SUBCOMMANDS = {"gencert", "setpass", "check", "serve"}


def _set_config_env(cfg) -> None:
    """Set CONFIG_PATH from settings.ini so utility commands can find config/secrets."""
    config_path = os.environ.get("CONFIG_PATH") or _get(cfg, "server", "config", "config.json")
    if not os.path.isabs(config_path):
        config_path = str(BASE / config_path)
    os.environ["CONFIG_PATH"] = config_path


def main() -> None:
    cfg = ensure_settings()

    # subcommand dispatcher: restart-orchestrator.exe <command> ...
    # (so gencert/setpass/check work from the exe on a control PC without Python)
    if len(sys.argv) > 1 and sys.argv[1] in SUBCOMMANDS:
        cmd = sys.argv.pop(1)  # drop the command; the rest go to the module
        _set_config_env(cfg)
        from app.console import setup_console
        setup_console()
        if cmd == "gencert":
            from app.gencert import main as m
            m()
            return
        if cmd == "setpass":
            from app.setpass import main as m
            m()
            return
        if cmd == "check":
            from app.check import main as m
            m()
            return
        # "serve" — just fall through to starting the server below

    host = os.environ.get("HOST") or _get(cfg, "server", "host", "0.0.0.0")
    port = int(os.environ.get("PORT") or _get(cfg, "server", "port", "8443"))
    config_path = os.environ.get("CONFIG_PATH") or _get(cfg, "server", "config", "config.json")
    if not os.path.isabs(config_path):
        config_path = str(BASE / config_path)

    # auth via environment (main.py reads ADMIN_USER/ADMIN_PASSWORD/AUTH_DISABLED)
    os.environ.setdefault("CONFIG_PATH", config_path)
    if _get(cfg, "auth", "auth_disabled", "0") == "1":
        os.environ["AUTH_DISABLED"] = "1"
    else:
        os.environ["ADMIN_USER"] = os.environ.get("ADMIN_USER") or _get(cfg, "auth", "admin_user", "admin")
        os.environ["ADMIN_PASSWORD"] = os.environ.get("ADMIN_PASSWORD") or _get(cfg, "auth", "admin_password", "")
        os.environ["SE_USER"] = os.environ.get("SE_USER") or _get(cfg, "auth", "se_user", "")
        os.environ["SE_PASSWORD"] = os.environ.get("SE_PASSWORD") or _get(cfg, "auth", "se_password", "")

    # HTTPS
    ssl_kwargs = {}
    if _get(cfg, "https", "enabled", "1") == "1":
        certfile = _get(cfg, "https", "certfile", "certs/server.crt")
        keyfile = _get(cfg, "https", "keyfile", "certs/server.key")
        certfile = certfile if os.path.isabs(certfile) else str(BASE / certfile)
        keyfile = keyfile if os.path.isabs(keyfile) else str(BASE / keyfile)
        if not (os.path.exists(certfile) and os.path.exists(keyfile)):
            print("HTTPS is enabled, but the certificate was not found. Create it with:")
            print("    python -m app.gencert --host <control-PC-IP>")
            print(f"Expected files:\n  {certfile}\n  {keyfile}")
            sys.exit(1)
        ssl_kwargs = {"ssl_certfile": certfile, "ssl_keyfile": keyfile}
        scheme = "https"
    else:
        scheme = "http"

    import uvicorn

    from app.main import app as asgi_app

    shown = host if host != "0.0.0.0" else "<this-machine-address>"
    print(f"Restart Orchestrator starting: {scheme}://{shown}:{port}")
    uvicorn.run(asgi_app, host=host, port=port, workers=1, **ssl_kwargs)


if __name__ == "__main__":
    main()
