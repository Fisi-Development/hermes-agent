"""Tests for hermes_cli/_scan_venv_blockers.py.

Tests call the real production functions (``main``, ``_redact_sensitive_cmdline``).
The detector is patched directly so no real process table interaction occurs.
"""

from __future__ import annotations

import builtins
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import agent.redact as redact_module
from hermes_cli._scan_venv_blockers import (
    _is_hermes_tools_mcp_command,
    _is_owned_mcp_bridge_executable,
    _is_pausable_gateway,
    _redact_sensitive_cmdline,
    main,
)


# ---------------------------------------------------------------------------
# main() — stdout, stderr, exit code (with patched detector)
# ---------------------------------------------------------------------------


def _psutil_fake() -> dict:
    """Return a sys.modules dict entry that makes psutil appear available."""
    return {"psutil": types.SimpleNamespace(Process=lambda *a: MagicMock())}






# ---------------------------------------------------------------------------
# _redact_sensitive_cmdline
# ---------------------------------------------------------------------------


def test_redact_long_flag_value_space_separated() -> None:
    """--token SECRET must preserve --token and emit --token <redacted>."""
    raw = "python.exe -m hermes_cli.main serve --token ghp_abc123 --host 10.0.0.1"
    result = _redact_sensitive_cmdline(raw)
    assert result == "python.exe -m hermes_cli.main serve --token <redacted>"
    assert "ghp_abc123" not in result




def test_redact_sensitive_text_failure_returns_fully_redacted() -> None:
    """When agent.redact.redact_sensitive_text raises, the entire result
    must equal '<redacted>' so PID and name still provide diagnostics."""
    with patch.object(
        redact_module,
        "redact_sensitive_text",
        side_effect=RuntimeError("no redactor"),
    ):
        result = _redact_sensitive_cmdline("python.exe --token abc123")

    assert result == "<redacted>"


def test_redact_session_key() -> None:
    """--session-key <identifier> must redact the value and everything after."""
    raw = "python.exe -m tui_gateway.slash_worker --session-key 20260712-abcdef --model test"
    result = _redact_sensitive_cmdline(raw)
    assert result == "python.exe -m tui_gateway.slash_worker --session-key <redacted>"


def test_redact_normal_host_port_profile_remain() -> None:
    raw = "python.exe -m hermes_cli.main serve --host 10.0.0.1 --port 9119 --profile work"
    result = _redact_sensitive_cmdline(raw)
    assert "10.0.0.1" in result
    assert "9119" in result
    assert "work" in result


def test_redact_no_sensitive_flags_is_noop() -> None:
    raw = "python.exe -m hermes_cli.main serve --host 127.0.0.1"
    assert _redact_sensitive_cmdline(raw) == raw


def test_redact_empty_string() -> None:
    assert _redact_sensitive_cmdline("") == ""


def test_redact_short_flags_not_redacted() -> None:
    """Short flags -t (toolset), -p (profile), -k are NOT redacted."""
    raw = "python.exe -m hermes_cli.main serve -t web -p default -k somearg"
    result = _redact_sensitive_cmdline(raw)
    assert result == raw  # short flags pass through unchanged


# ---------------------------------------------------------------------------
# _is_pausable_gateway — the gateway exemption
#
# `hermes-setup` always invokes `hermes update --yes --gateway`, whose
# `_pause_windows_gateways_for_update()` stops running gateways itself. The
# Desktop preflight must therefore not report gateway launcher/worker chains
# as blockers — doing so aborts the handoff before the component that can
# handle them ever runs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmdline",
    [
        # venv-side launcher, exactly as the scheduled task spawns it
        r"C:\Users\u\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
        " -m hermes_cli.main gateway run --replace",
        # uv-side worker re-running the same argv (quoted exe, double space)
        r'"C:\Users\u\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe"'
        "  -m hermes_cli.main gateway run --replace",
        # profile-scoped gateway
        "python.exe -m hermes_cli.main --profile work gateway run",
        # a profile literally NAMED "gateway" — the profile value must not
        # shadow the subcommand token (the hand-rolled matcher regressed this)
        "python.exe -m hermes_cli.main --profile gateway gateway run",
        "python.exe -m hermes_cli.main -p gateway gateway run",
        # bare `gateway` defaults to `run` (mirrors the canonical matcher)
        "python.exe -m hermes_cli.main gateway",
        # case variations survive
        "PYTHON.EXE -m hermes_cli.main GATEWAY RUN",
    ],
)
def test_is_pausable_gateway_accepts_gateway_run_chains(cmdline: str) -> None:
    assert _is_pausable_gateway(cmdline) is True


@pytest.mark.parametrize(
    "cmdline",
    [
        # desktop backend: no pause machinery downstream, must keep blocking
        "python.exe -m hermes_cli.main serve --host 127.0.0.1 --port 8756",
        # other gateway subcommands are not running gateways
        "python.exe -m hermes_cli.main gateway stop",
        "python.exe -m hermes_cli.main gateway status",
        "python.exe -m hermes_cli.main gateway install",
        # operator REPL / stray script
        "python.exe",
        "python.exe myscript.py gateway run",  # not a hermes_cli.main invocation
        "",
    ],
)
def test_is_pausable_gateway_rejects_everything_else(cmdline: str) -> None:
    assert _is_pausable_gateway(cmdline) is False


def _run_main_with_detector(monkeypatch, capsys, matches, *, argv=None):
    """Run main() with the process detector patched to return *matches*."""
    for name, mod in _psutil_fake().items():
        monkeypatch.setitem(sys.modules, name, mod)
    import hermes_cli.main as cli_main

    monkeypatch.setattr(cli_main, "_detect_venv_python_processes", lambda **_kwargs: matches)
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        (root / "venv").mkdir()
        (root / "hermes_cli").mkdir()
        monkeypatch.setattr(sys, "prefix", str(root / "venv"))
        argv = argv or ["--root", str(root)]
        with pytest.raises(SystemExit) as excinfo:
            main(argv)
        out = capsys.readouterr().out
        return excinfo.value.code, json.loads(out)


def test_main_requires_an_explicit_target_root(monkeypatch, capsys):
    for name, mod in _psutil_fake().items():
        monkeypatch.setitem(sys.modules, name, mod)

    with pytest.raises(SystemExit) as excinfo:
        main([])

    data = json.loads(capsys.readouterr().out)
    assert excinfo.value.code == 1
    assert data["ok"] is False


def test_main_exempts_gateway_chain_but_keeps_other_holders(monkeypatch, capsys):
    """A gateway launcher/worker pair alone must scan clear; a non-gateway
    holder alongside it must still block (and be the only reported PID)."""
    gateway_launcher = (
        12,
        "python.exe",
        r"C:\x\venv\Scripts\python.exe -m hermes_cli.main gateway run --replace",
    )
    gateway_worker = (
        34,
        "python.exe",
        r'"C:\u\uv\python\python.exe"  -m hermes_cli.main gateway run --replace',
    )
    stray_repl = (56, "python.exe", r"C:\x\venv\Scripts\python.exe")

    # Gateway chain only → clear
    code, data = _run_main_with_detector(
        monkeypatch, capsys, [gateway_launcher, gateway_worker]
    )
    assert code == 0
    assert data["ok"] is True
    assert data["blocked"] is False
    assert data["processes"] == []
    assert data["pausable_gateways"] == 2

    # Gateway chain + stray REPL → blocked, reporting only the REPL
    code, data = _run_main_with_detector(
        monkeypatch, capsys, [gateway_launcher, gateway_worker, stray_repl]
    )
    assert code == 0
    assert data["blocked"] is True
    assert [p["pid"] for p in data["processes"]] == [56]
    assert data["pausable_gateways"] == 2


def test_main_rereads_truncated_runtime_gateway_argv(monkeypatch, capsys):
    import hermes_cli._scan_venv_blockers as scan

    cropped_runtime = (34, "python.exe", r"C:\target\.hermes-runtime\python\generation\cpython-3.11-windows-x86_64")
    monkeypatch.setattr(
        scan,
        "_live_pausable_gateway",
        lambda pid, _display_cmdline: pid == cropped_runtime[0],
    )

    code, data = _run_main_with_detector(monkeypatch, capsys, [cropped_runtime])

    assert code == 0
    assert data["blocked"] is False
    assert data["pausable_gateways"] == 1


def test_main_keeps_a_truncated_non_gateway_runtime_blocking(monkeypatch, capsys):
    import hermes_cli._scan_venv_blockers as scan

    cropped_runtime = (35, "python.exe", r"C:\target\.hermes-runtime\python\generation\cpython-3.11-windows-x86_64")
    monkeypatch.setattr(scan, "_live_pausable_gateway", lambda _pid, _display_cmdline: False)

    code, data = _run_main_with_detector(monkeypatch, capsys, [cropped_runtime])

    assert code == 0
    assert data["blocked"] is True
    assert [proc["pid"] for proc in data["processes"]] == [35]


def test_main_desktop_serve_backend_still_blocks(monkeypatch, capsys):
    """The desktop's own `serve` backend has no downstream pause — it must
    keep blocking exactly as before the exemption."""
    serve = (
        78,
        "python.exe",
        r"C:\x\venv\Scripts\python.exe -m hermes_cli.main serve --host 127.0.0.1",
    )
    code, data = _run_main_with_detector(monkeypatch, capsys, [serve])
    assert code == 0
    assert data["blocked"] is True
    assert [p["pid"] for p in data["processes"]] == [78]
    assert data["pausable_gateways"] == 0


def test_main_keeps_gateway_owned_descendant_blocking(monkeypatch, capsys):
    """A dead gateway worker can orphan this holder before its launcher dies.

    Exempting by ancestry made the detached updater fail without a visible
    Desktop error because the downstream guard only recognizes direct gateway
    command lines.  Only direct gateway launchers are safe to defer.
    """
    descendant = (79, "python.exe", r"C:\\x\\venv\\Scripts\\python.exe -m some.child")
    code, data = _run_main_with_detector(monkeypatch, capsys, [descendant])
    assert code == 0
    assert data["blocked"] is True
    assert [proc["pid"] for proc in data["processes"]] == [79]


def test_mcp_bridge_command_requires_the_exact_stdio_launch() -> None:
    assert _is_hermes_tools_mcp_command(
        ["python.exe", "-m", "agent.transports.hermes_tools_mcp_server"]
    )
    assert not _is_hermes_tools_mcp_command(
        ["python.exe", "-m", "agent.transports.hermes_tools_mcp_server", "--verbose"]
    )
    assert not _is_hermes_tools_mcp_command(
        ["python.exe", "-m", "agent.transports.codex_app_server"]
    )


def test_mcp_bridge_accepts_only_the_venv_and_managed_runtime_interpreters(tmp_path: Path) -> None:
    root = tmp_path / "install"
    venv_python = root / "venv" / "Scripts" / "python.exe"
    managed_runtime = root / ".hermes-runtime" / "python" / "generation" / "python.exe"
    unrelated_runtime = root / ".hermes-runtime" / "node" / "node.exe"
    other_install_runtime = tmp_path / "other-install" / ".hermes-runtime" / "python" / "python.exe"

    assert _is_owned_mcp_bridge_executable(str(venv_python).lower(), root)
    assert _is_owned_mcp_bridge_executable(str(managed_runtime).lower(), root)
    assert not _is_owned_mcp_bridge_executable(str(unrelated_runtime).lower(), root)
    assert not _is_owned_mcp_bridge_executable(str(other_install_runtime).lower(), root)


def test_main_reports_mcp_bridge_as_a_blocker_for_explicit_desktop_consent(monkeypatch, capsys):
    import hermes_cli._scan_venv_blockers as scan

    bridge = (
        90,
        "python.exe",
        r"C:\target\venv\Scripts\python.exe -m agent.transports.hermes_tools_mcp_server",
    )
    monkeypatch.setattr(
        scan,
        "_live_mcp_bridge",
        lambda pid, _root: {
            "pid": pid,
            "name": "python.exe",
            "cmdline": bridge[2],
            "created_at": 123.5,
        },
    )

    code, data = _run_main_with_detector(
        monkeypatch,
        capsys,
        [bridge],
    )

    assert code == 0
    assert data["blocked"] is True
    assert data["processes"] == []
    assert data["mcp_bridges"] == [
        {
            "pid": 90,
            "name": "python.exe",
            "cmdline": bridge[2],
            "created_at": 123.5,
        }
    ]


def test_main_reports_both_sides_of_the_mcp_trampoline_for_consent(monkeypatch, capsys):
    import hermes_cli._scan_venv_blockers as scan

    launcher = (90, "python.exe", r"C:\target\venv\Scripts\python.exe -m agent.transports.hermes_tools_mcp_server")
    worker = (
        91,
        "python.exe",
        r"C:\target\.hermes-runtime\python\generation\python.exe -m agent.transports.hermes_tools_mcp_server",
    )
    monkeypatch.setattr(
        scan,
        "_live_mcp_bridge",
        lambda pid, _root: {
            "pid": pid,
            "name": "python.exe",
            "cmdline": launcher[2] if pid == launcher[0] else worker[2],
            "created_at": 123.5,
        }
        if pid in {launcher[0], worker[0]}
        else None,
    )

    code, data = _run_main_with_detector(monkeypatch, capsys, [launcher, worker])

    assert code == 0
    assert data["processes"] == []
    assert [bridge["pid"] for bridge in data["mcp_bridges"]] == [90, 91]


def test_main_keeps_mixed_mcp_and_unknown_holders_blocking(monkeypatch, capsys):
    import hermes_cli._scan_venv_blockers as scan

    bridge = (90, "python.exe", r"C:\target\venv\Scripts\python.exe -m agent.transports.hermes_tools_mcp_server")
    repl = (91, "python.exe", r"C:\target\venv\Scripts\python.exe")
    monkeypatch.setattr(
        scan,
        "_live_mcp_bridge",
        lambda pid, _root: (
            {"pid": pid, "name": "python.exe", "cmdline": bridge[2], "created_at": 123.5}
            if pid == 90
            else None
        ),
    )

    code, data = _run_main_with_detector(
        monkeypatch,
        capsys,
        [bridge, repl],
    )

    assert code == 0
    assert data["blocked"] is True
    assert [proc["pid"] for proc in data["processes"]] == [91]
    assert [proc["pid"] for proc in data["mcp_bridges"]] == [90]
