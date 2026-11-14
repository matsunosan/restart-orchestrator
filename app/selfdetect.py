"""Detect the host the orchestrator runs on, so it does not reboot itself."""
from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor

import psutil

from .config import AppConfig


def local_addresses() -> set[str]:
    addresses: set[str] = set()
    for addrs in psutil.net_if_addrs().values():
        for addr in addrs:
            if addr.family in (socket.AF_INET, socket.AF_INET6):
                addresses.add(addr.address.split("%")[0])
    return addresses


def local_names() -> set[str]:
    names = {"localhost", socket.gethostname().lower(), socket.getfqdn().lower()}
    names |= {n.split(".")[0] for n in list(names)}
    return names


def resolve(hostname: str) -> set[str]:
    try:
        return {info[4][0].split("%")[0] for info in socket.getaddrinfo(hostname, None)}
    except OSError:
        return set()


def find_self_hosts(config: AppConfig) -> dict[str, str]:
    """{host_id: exclusion reason}. DNS resolution runs in parallel."""
    settings = config.settings
    hosts = config.all_hosts()
    excluded = {hid: "excluded manually (settings.exclude_host_ids)" for hid in settings.exclude_host_ids}
    if not settings.self_detection:
        return excluded

    ips, names = local_addresses(), local_names()
    with ThreadPoolExecutor(max_workers=16) as pool:
        resolved = dict(zip([h.id for h in hosts], pool.map(lambda h: resolve(h.hostname), hosts), strict=True))

    for host in hosts:
        if host.id in excluded:
            continue
        hostname = host.hostname.lower()
        if hostname in names:
            excluded[host.id] = "this is the orchestrator server (hostname matches)"
            continue
        addrs = resolved[host.id]
        if any(ipaddress.ip_address(a).is_loopback for a in addrs):
            excluded[host.id] = "this is the orchestrator server (loopback address)"
        elif addrs & ips:
            excluded[host.id] = f"this is the orchestrator server (address {sorted(addrs & ips)[0]})"
    return excluded
