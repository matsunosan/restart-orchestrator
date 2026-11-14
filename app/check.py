"""Check real hosts without rebooting.

    python -m app.check                 # all hosts from config.json
    python -m app.check ws-win-01 db-01 # selected ones
    python -m app.check --config other.json

Always works with real machines, even if the config has dry_run=true.
Changes nothing on the hosts: only connects and reads state.
Exit code 1 if at least one check fails.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .config import load_config
from .console import setup_console
from .logging_setup import setup_logging
from .orchestrator import Orchestrator
from .selfdetect import find_self_hosts
from .state import StateStore

MARK = {"ok": "  OK ", "warn": " WARN", "error": " FAIL "}


async def main_async(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    config.settings.dry_run = False
    for warning in config.load_warnings:
        print(f"[configuration] {warning}")
    store = StateStore(config)
    orchestrator = Orchestrator(config, store)
    orchestrator.excluded = await asyncio.to_thread(find_self_hosts, config)

    hosts = [config.get_host(h) for h in args.hosts] if args.hosts else config.all_hosts()
    print(f"Checking {len(hosts)} host(s), no reboots…\n")
    results = await asyncio.gather(*(orchestrator.check_host(h) for h in hosts))

    failed = 0
    for host, result in zip(hosts, results, strict=True):
        state = "passed" if result["state"] == "ok" else "FAILED"
        failed += result["state"] != "ok"
        print(f"{host.name} ({host.id}, {host.os}, {host.user}@{host.hostname}) — {state}")
        for item in result["items"]:
            print(f"   [{MARK[item['level']]}] {item['label']}: {item['detail']}")
        print()
    print(f"Total: passed {len(hosts) - failed} of {len(hosts)}")
    return 1 if failed else 0


def main() -> None:
    setup_console()
    parser = argparse.ArgumentParser(description="Check host access without rebooting")
    parser.add_argument("hosts", nargs="*", help="host ids (default: all)")
    parser.add_argument("--config", default=os.environ.get("CONFIG_PATH", "config.json"))
    args = parser.parse_args()
    if os.environ.get("LOG_LEVEL", "").upper() == "DEBUG":
        setup_logging("logs/check.log", level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.CRITICAL)
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
