# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Changed
- Project translated to English: UI, log and status messages, code comments, docs.
- Shipped configuration is demo data only; example credentials are placeholders.

## [0.20.0] - 2026-11-02
### Added
- Selective restart: pick machines with checkboxes and restart only those
  (`POST /api/stack/restart` accepts `{"host_ids": [...]}`).

## [0.19.0] - 2026-11-02
### Added
- Two roles enforced on the server: `admin` (everything) and `SE` (view, check, restart).
  Adding, editing and deleting machines is admin-only.
- Per-OS preparation checklist in the "Add machine" form.

## [0.17.0] - 2026-11-02
### Added
- Single-file Windows executable (PyInstaller) with built-in `gencert`, `setpass` and `check`
  subcommands, configured through `settings.ini`.
### Security
- The master password is never stored: it is entered in the web UI, kept in memory only,
  and wiped after two hours of inactivity. It can be changed only by someone who knows the old one.

## [0.16.0] - 2026-11-02
### Added
- Login page with session cookies (12-hour sessions) replacing browser Basic Auth; log-out button.
### Fixed
- API tests updated for the session login flow.

## [0.15.0] - 2026-11-02
### Added
- HTTPS for the web UI with a generated self-signed certificate (`python -m app.gencert`).

## [0.14.0] - 2026-11-02
### Added
- Optional encryption of machine passwords with a master password
  (AES-256-GCM, scrypt key derivation) and the `setpass` CLI: `enable`, `disable`, `set`, `rekey`, `status`.
- Lock banner in the UI; SSH-key machines keep working while password-based ones are locked.

## [0.13.0] - 2026-10-12
### Added
- SSH key authentication for Linux and macOS hosts, with passwordless sudo limited to specific commands.

## [0.12.x] - 2026-10-12
### Changed
- Group is chosen from a fixed list instead of free text, so a typo no longer creates a new column.
### Fixed
- Strict IPv4 validation on the server and live in the form: malformed addresses
  (too many digits, missing octets, octets above 255) are rejected.

## [0.11.0] - 2026-10-12
### Added
- Machine editor in the web UI: add, edit and delete hosts with full field validation.
  Passwords go to `secrets.json` only; files are written atomically.

## [0.10.0] - 2026-10-12
### Added
- `app_health_port`: application readiness detected by its TCP port opening, independent of WinRM/SSH.
### Changed
- The login-wait phase is skipped when the application check already proves the user session exists.

## [0.9.x] - 2026-10-11
### Fixed
- Windows status stuck on "Booting" for minutes: WinRM comes up long after the OS does.
  Boot detection now uses ICMP ping or the management port, with a fast TCP pre-check
  and short timeouts on every polling call.
### Added
- Per-call timing in DEBUG logs.

## [0.8.0] - 2026-10-11
### Changed
- Status messages update every second during the boot, login and application phases.

## [0.7.0] - 2026-10-11
### Added
- `app_process_name` for Windows: verify the application by its process when a task or
  batch file starts it and exits.

## [0.6.x] - 2026-10-11
### Fixed
- Garbled non-ASCII text in the Windows console and in PowerShell output (forced UTF-8).
- Linux kiosk autostart file name is taken from the host's `app_service_name`.
### Added
- `desktop_autostart` service type for Linux kiosk applications that need a graphical session.

## [0.5.x] - 2026-10-11
### Added
- `secrets.json` with per-host passwords, password sudo via stdin, autologon checks
  (`requires_user_session`), self-detection so the orchestrator never reboots its own machine,
  and a non-destructive check (`python -m app.check` and the "Check" buttons).

## [0.1.0 – 0.4.0] - 2026-10-11
### Added
- Initial beta: staged stack restart by groups, Linux/macOS over SSH and Windows over WinRM,
  autostart guarantee, boot-marker reboot confirmation, fail-safe policies, real-time UI over
  Server-Sent Events, simulation mode, Zabbix status, webhook alerts, tests and CI.
