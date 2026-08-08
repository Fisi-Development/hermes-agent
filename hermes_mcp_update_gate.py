"""Minimal, stdlib-only update gate for the Codex Hermes-tools bridge.

This module is intentionally outside :mod:`agent`: ``python -m
agent.transports.hermes_tools_mcp_server`` imports ``agent.__init__`` before
the server module can run.  On Windows that import preloads a native extension
from the mutable venv, which is already too late to protect an in-place update.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

_MARKER_NAME = ".hermes-venv-quiesce"
_MAX_AGE_SECONDS = 20 * 60
_MCP_MAIN_MODULE = "agent.transports.hermes_tools_mcp_server"


def _pid_alive(pid: int) -> bool:
    """Return whether *pid* is live, treating access denial as live."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
        open_process.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_bool
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        get_exit_code.restype = ctypes.c_bool

        process = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
        try:
            exit_code = ctypes.c_ulong()
            if not get_exit_code(process, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            close_handle(process)
    except Exception:
        return False


def marker_path(hermes_home: str | Path | None = None) -> Path:
    """Return the bridge-specific marker without importing Hermes code."""
    if hermes_home is None:
        hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home is None:
        # Keep this stdlib bootstrap aligned with get_process_hermes_home():
        # Desktop's Windows marker lives under LOCALAPPDATA, not ~/.hermes.
        hermes_home = (
            Path(os.environ["LOCALAPPDATA"]) / "hermes"
            if os.name == "nt" and os.environ.get("LOCALAPPDATA")
            else Path.home() / ".hermes"
        )
    return Path(hermes_home) / _MARKER_NAME


def live_quiesce_marker(marker: str | Path | None = None) -> bool:
    """Return True for a fresh marker whose declared owner is still live."""
    try:
        path = marker_path() if marker is None else Path(marker)
        pid_line, started_line, *_ = path.read_text(encoding="utf-8").splitlines()
        pid = int(pid_line.strip())
        age = time.time() - float(started_line.strip())
    except (OSError, ValueError):
        return False
    return 0 <= age <= _MAX_AGE_SECONDS and _pid_alive(pid)


def should_quiesce_mcp_bridge() -> bool:
    """True only while executing the legacy MCP module under a live gate."""
    # ``runpy`` imports the parent package before it installs the target
    # module's __spec__ in __main__, so __main__.__spec__ is still None at the
    # only point where this gate can protect jiter_preload. orig_argv retains
    # the original ``-m <module>`` pair throughout that bootstrap.
    argv = getattr(sys, "orig_argv", sys.argv)
    return any(
        token == "-m" and index + 1 < len(argv) and argv[index + 1] == _MCP_MAIN_MODULE
        for index, token in enumerate(argv)
    ) and live_quiesce_marker()
