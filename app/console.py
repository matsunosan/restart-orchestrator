"""Correct Unicode output in the Windows console.

The Windows console uses CP866/CP1251 by default, which turns non-ASCII text
into garbled "mojibake". Here we switch stdout/stderr to UTF-8 and change the
console code page. On Linux/macOS we do nothing.
"""
from __future__ import annotations

import sys


def setup_console() -> None:
    if sys.platform != "win32":
        return
    # 1. Switch the console code page to UTF-8 (65001)
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    # 2. Make Python itself write in UTF-8
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
