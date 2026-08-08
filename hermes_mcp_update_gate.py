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
    home = hermes_home or os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
    return Path(home) / _MARKER_NAME


def live_quiesce_marker(hermes_home: str | Path | None = None) -> bool:
    """Return True for a fresh marker whose declared owner is still live."""
    try:
        pid_line, started_line, *_ = marker_path(hermes_home).read_text(encoding="utf-8").splitlines()
        pid = int(pid_line.strip())
        age = time.time() - float(started_line.strip())
    except (OSError, ValueError):
        return False
    return 0 <= age <= _MAX_AGE_SECONDS and _pid_alive(pid)


def should_quiesce_mcp_bridge() -> bool:
    """True only while executing the legacy MCP module under a live gate."""
    main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    return getattr(main_spec, "name", None) == _MCP_MAIN_MODULE and live_quiesce_marker()
