"""Opening a link or a local page from inside KiCad's plugin process.

Python's :mod:`webbrowser` drives the browser through AppleScript on macOS
(``osascript``), and in a process KiCad starts that can fail without a word:
``webbrowser.open`` returns False and nothing appears. On 2026-09-26 the "Get a
free key" button did nothing on KiCad 10.0.6 for exactly that reason. So wx's
own call is tried first (NSWorkspace on macOS, the shell on Windows,
``xdg-open`` on Linux), then the platform's opener command, then
:mod:`webbrowser`. The caller learns whether anything reported success and can
show the link for copying when nothing did.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Any, Callable

log = logging.getLogger(__name__)


def _command(url: str, platform: str) -> list | None:
    if platform == "darwin":
        return ["open", url]
    if platform.startswith("win"):
        return None  # os.startfile, below
    return ["xdg-open", url]


def open_url(
    url: str,
    *,
    wx_module: Any = None,
    platform: str | None = None,
    run: Callable[..., Any] = subprocess.run,
    startfile: Callable[[str], Any] | None = None,
    fallback: Callable[[str], bool] | None = None,
) -> bool:
    """True when something reported opening *url*; False when every way failed."""
    platform = platform or sys.platform
    if wx_module is not None:
        try:
            if wx_module.LaunchDefaultBrowser(url):
                return True
            log.warning("wx could not open the browser; trying the system opener")
        except Exception as exc:  # noqa: BLE001 — try the next way
            log.warning("wx could not open the browser (%s); trying the system opener", exc)

    command = _command(url, platform)
    try:
        if command is None:
            (startfile or os.startfile)(url)  # type: ignore[attr-defined]
            return True
        done = run(command, check=False, capture_output=True, timeout=15)
        if getattr(done, "returncode", 1) == 0:
            return True
        log.warning("%s exited with %s", command[0], getattr(done, "returncode", None))
    except Exception as exc:  # noqa: BLE001 — try the next way
        log.warning("the system opener failed (%s)", exc)

    try:
        if fallback is None:
            import webbrowser

            fallback = webbrowser.open
        return bool(fallback(url))
    except Exception as exc:  # noqa: BLE001
        log.warning("webbrowser failed (%s)", exc)
        return False
