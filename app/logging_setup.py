"""Logging: console + a rotating file.

Line format: 2026-10-11 11:03:00.306 +00:00 [INF] message
Levels are abbreviated: [INF] info, [WRN] warning, [ERR] error, [DBG] debug.
Time is local with a zone offset (+00:00), with milliseconds.
"""
from __future__ import annotations

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LEVEL_TAG = {
    logging.DEBUG: "DBG",
    logging.INFO: "INF",
    logging.WARNING: "WRN",
    logging.ERROR: "ERR",
    logging.CRITICAL: "ERR",
}


class OrchestratorFormatter(logging.Formatter):
    """2026-10-11 11:03:00.306 +00:00 [INF] message"""

    def format(self, record: logging.LogRecord) -> str:
        # local time with a zone offset, milliseconds
        dt = datetime.fromtimestamp(record.created).astimezone()
        ms = int(record.msecs)
        offset = dt.strftime("%z")  # e.g. +0000
        offset = f"{offset[:3]}:{offset[3:]}" if offset else "+00:00"
        stamp = dt.strftime("%Y-%m-%d %H:%M:%S") + f".{ms:03d} {offset}"
        tag = _LEVEL_TAG.get(record.levelno, "INF")
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return f"{stamp} [{tag}] {msg}"


def setup_logging(log_file: str, level: int = logging.INFO) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    fmt = OrchestratorFormatter()
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_orchestrator", False):
            root.removeHandler(handler)
    file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    console = logging.StreamHandler()
    for handler in (file_handler, console):
        handler.setFormatter(fmt)
        handler._orchestrator = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
