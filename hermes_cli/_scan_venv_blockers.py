"""``hermes_cli/_scan_venv_blockers.py`` — Standalone venv-process scan for JSON consumption.

Invoked by the Desktop Electron app::

    venv\\Scripts\\python.exe -m hermes_cli._scan_venv_blockers

Exits 0 for valid clear or blocked results.  Non-zero exit signals probe
failure (the detector itself crashed, psutil unavailable, etc.).  Exactly
one JSON document on stdout; diagnostics on stderr only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NoReturn

# Long CLI flags whose argument value must be redacted from the cmdline.
_SENSITIVE_LONG_FLAGS: list[str] = [
    "--token",
    "--api-key",
    "--password",
    "--secret",
    "--authorization",
    "--access-key",
    "--private-key",
    "--session-key",
]


def _probe_fail_json() -> str:
    """Return the standard probe-failure JSON document."""
    return json.dumps({"ok": False, "blocked": False, "processes": []})


def _emit_probe_fail(diagnostic: str) -> NoReturn:
    """Print one JSON to stdout, diagnostic to stderr, exit non-zero."""
    print(_probe_fail_json())
    print(diagnostic, file=sys.stderr)
    sys.exit(1)


def _find_flag(text: str, flag: str) -> int:
    """Return the index of *flag* when it starts the string or follows a space.

    Returns -1 when not found.  This avoids matching ``--token`` inside an
    embedded token or path like ``/some--token-thing``.
    """
    low = text.lower()
    fl = flag.lower()
    pos = 0
    while True:
        idx = low.find(fl, pos)
        if idx == -1:
            return -1
        if idx == 0 or text[idx - 1] == " ":
            return idx
        pos = idx + 1


def _redact_sensitive_cmdline(cmdline: str) -> str:
    """Apply generic secret redaction then long-flag redaction.

    If the generic redactor itself fails, return ``"<redacted>"`` — the PID
    and process name still provide actionable diagnostics.
    """
    # Generic pass: the project's shared secret redactor.
    try:
        from agent.redact import redact_sensitive_text  # noqa: PLC0415

        cmdline = redact_sensitive_text(cmdline, force=True)
    except Exception:
        return "<redacted>"

    # Conservative long-flag pass: preserve the flag name, replace the value
    # and everything after it with ``<redacted>``.  Short flags (-t, -k, -p)
    # are intentionally not redacted — they are ambiguous and may be useful
    # diagnostics (toolset, port, profile).
    earliest = len(cmdline)
    for flag in _SENSITIVE_LONG_FLAGS:
        # --flag=value  →  preserve "--flag="
        idx = _find_flag(cmdline, flag + "=")
        if idx != -1 and idx + len(flag) + 1 < earliest:
            earliest = idx + len(flag) + 1
        # --flag value  →  preserve "--flag "
        idx = _find_flag(cmdline, flag + " ")
        if idx != -1 and idx + len(flag) + 1 < earliest:
            earliest = idx + len(flag) + 1

    if earliest < len(cmdline):
        return cmdline[:earliest] + "<redacted>"
    return cmdline


def _is_pausable_gateway(cmdline: str) -> bool:
    """Return True when *cmdline* is a gateway process the updater can pause.

    A running gateway shows up in the venv-holder scan as one or both halves
    of its launcher/worker chain (``venv\\Scripts\\python.exe -m
    hermes_cli.main gateway run`` and the uv-side interpreter re-running the
    same argv). Reporting those as blockers dead-ends the Desktop update:
    the preflight aborts with ``venv-blocked`` *before* spawning
    ``hermes-setup``, so the CLI updater's own
    ``_pause_windows_gateways_for_update()`` — which exists precisely to
    stop these processes (and is always active: ``hermes-setup`` invokes
    ``hermes update --yes --gateway``) — never gets the chance to run.

    Only gateway invocations are exempted. Anything else running from the
    venv (an operator's REPL, a stray script, a ``serve`` backend that
    survived the desktop's own teardown) has no pause machinery downstream
    and must keep blocking the handoff.

    Delegates to ``gateway.status.looks_like_gateway_command_line`` — the
    canonical ``gateway run`` matcher (profile-selector aware, shlex
    tokenization, ``run``-only) — so this exemption, the pause discovery,
    and the updater's guard fallback all share one parser. A hand-rolled
    token scan here regressed ``--profile gateway gateway run``: the profile
    *value* shadowed the subcommand token. An import failure counts as
    not-pausable — the scan then reports the process as a blocker, which is
    exactly the pre-exemption behavior.
    """
    try:
        from gateway.status import looks_like_gateway_command_line  # noqa: PLC0415
    except Exception:
        return False
    return looks_like_gateway_command_line(cmdline)


def _has_pausable_gateway_ancestor(pid: int) -> bool:
    """Return True only when a fully readable ancestor is ``gateway run``.

    Gateway-owned venv grandchildren are paused by the downstream CLI updater,
    so reporting them here would abort before that owner can run. Any missing
    process or unreadable command line remains a blocker.
    """
    try:
        import psutil  # noqa: PLC0415

        parents = psutil.Process(int(pid)).parents()
    except Exception:
        return False

    for parent in parents:
        try:
            parts = parent.cmdline()
        except Exception:
            return False
        if not parts:
            return False
        if _is_pausable_gateway(" ".join(str(part) for part in parts)):
            return True
    return False


def _venv_prefix(root: Path) -> str:
    try:
        return str((root / "venv").resolve()).lower().rstrip(os.sep) + os.sep
    except OSError:
        return str(root / "venv").lower().rstrip(os.sep) + os.sep


def _is_hermes_tools_mcp_command(parts: list[str]) -> bool:
    """Return True only for the migration's argument-free stdio MCP launch."""
    try:
        module_index = parts.index("-m")
    except ValueError:
        return False
    return (
        module_index + 2 == len(parts)
        and parts[module_index + 1] == "agent.transports.hermes_tools_mcp_server"
    )


def _live_mcp_bridge_process(pid: int, root: Path):
    """Return a re-verified bridge snapshot, or None for every mismatch.

    The scan's display command is deliberately truncated/redacted, so it must
    never be used to authorize a bridge-specific action. This re-reads the
    live process and captures its creation time for the PID-reuse guard used
    by ``--terminate-mcp-bridge``.
    """
    try:
        import psutil  # noqa: PLC0415

        proc = psutil.Process(int(pid))
        exe = proc.exe()
        parts = [str(part) for part in proc.cmdline()]
        created_at = float(proc.create_time())
        name = proc.name()
    except Exception:
        return None

    if not exe or not parts:
        return None
    try:
        exe_norm = str(Path(exe).resolve()).lower()
    except (OSError, ValueError):
        exe_norm = str(exe).lower()
    if not exe_norm.startswith(_venv_prefix(root)) or not _is_hermes_tools_mcp_command(parts):
        return None
    return proc, {
        "pid": int(pid),
        "name": str(name or Path(exe).name),
        "cmdline": " ".join(parts),
        "created_at": created_at,
    }


def _live_mcp_bridge(pid: int, root: Path) -> dict | None:
    """Return a serializable snapshot of a live, exact MCP bridge."""
    live = _live_mcp_bridge_process(pid, root)
    return live[1] if live is not None else None


def _terminate_mcp_bridge(pid: int, created_at: float, root: Path) -> bool:
    """Terminate one consented, still-identical bridge process without /T."""
    live = _live_mcp_bridge_process(pid, root)
    if live is None:
        return False
    proc, snapshot = live
    if abs(float(snapshot["created_at"]) - created_at) > 0.01:
        return False
    try:
        proc.kill()
    except Exception:
        return False
    return True


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--terminate-mcp-bridge", type=int)
    parser.add_argument("--created-at", type=float)
    args = parser.parse_args([] if argv is None else argv)
    if (args.terminate_mcp_bridge is None) != (args.created_at is None):
        parser.error("--terminate-mcp-bridge and --created-at must be used together")
    return args


def _canonical(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def _validated_root(root: Path | None) -> Path:
    """Validate that the scanner interpreter belongs to the requested install."""
    if root is None:
        _emit_probe_fail("--root is required")
    root = root.resolve()
    venv = root / "venv"
    if not venv.is_dir() or not ((root / "hermes_cli").is_dir() or (root / "pyproject.toml").is_file()):
        _emit_probe_fail(f"invalid Hermes install root: {root}")
    if _canonical(Path(sys.prefix)) != _canonical(venv):
        _emit_probe_fail(
            f"scan interpreter venv {Path(sys.prefix).resolve()} does not match requested root venv {venv.resolve()}"
        )
    return root


def main(argv: list[str] | None = None) -> None:
    """Entry point.  Prints one JSON doc to stdout.  Exits 0 for valid scan."""
    try:
        args = _parse_args(argv)
    except SystemExit:
        _emit_probe_fail("invalid scan arguments")

    try:
        import psutil  # noqa: PLC0415, F401
    except Exception as exc:
        _emit_probe_fail(f"psutil is not available: {exc}")

    try:
        from hermes_cli.main import _detect_venv_python_processes  # noqa: PLC0415

        root = _validated_root(args.root)
        if args.terminate_mcp_bridge is not None:
            terminated = _terminate_mcp_bridge(args.terminate_mcp_bridge, args.created_at, root)
            print(json.dumps({"ok": True, "terminated": terminated}))
            sys.exit(0)

        matches = _detect_venv_python_processes(root=root)
    except Exception as exc:
        _emit_probe_fail(f"scan aborted: {exc}")

    processes = []
    mcp_bridges = []
    exempted = 0
    for pid, name, cmdline in matches:
        if _is_pausable_gateway(cmdline) or _has_pausable_gateway_ancestor(pid):
            exempted += 1
            continue
        bridge = _live_mcp_bridge(pid, root)
        if bridge is not None:
            bridge["cmdline"] = _redact_sensitive_cmdline(bridge["cmdline"])
            mcp_bridges.append(bridge)
            continue
        processes.append(
            {
                "pid": pid,
                "name": name,
                "cmdline": _redact_sensitive_cmdline(cmdline),
            }
        )
    data = {
        "ok": True,
        "blocked": bool(processes or mcp_bridges),
        "processes": processes,
        # These are still blockers. Electron may only offer an explicit
        # consent path when this is the sole non-gateway holder class.
        "mcp_bridges": mcp_bridges,
        # Diagnostic only: gateway processes present but not counted as
        # blockers because the downstream updater pauses their whole trees.
        "pausable_gateways": exempted,
    }
    try:
        from hermes_cli.main import PROJECT_ROOT  # noqa: PLC0415

        data["code_root"] = str(Path(PROJECT_ROOT).resolve())
    except Exception:
        pass
    print(json.dumps(data))
    sys.exit(0)


if __name__ == "__main__":
    main(sys.argv[1:])
