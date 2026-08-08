"""Regression tests for the pre-``agent`` Codex MCP update gate."""

from __future__ import annotations

import sys
import time
import os
import subprocess
from pathlib import Path

import hermes_mcp_update_gate as gate


def test_live_marker_accepts_only_a_fresh_live_owner(monkeypatch, tmp_path):
    marker = tmp_path / ".hermes-venv-quiesce"
    marker.write_text(f"123\n{int(time.time())}\n", encoding="utf-8")
    monkeypatch.setattr(gate, "_pid_alive", lambda pid: pid == 123)
    assert gate.live_quiesce_marker(marker) is True

    marker.write_text("123\n1\n", encoding="utf-8")
    assert gate.live_quiesce_marker(marker) is False


def test_legacy_module_is_gated_before_agent_import(monkeypatch, tmp_path):
    marker = tmp_path / ".hermes-venv-quiesce"
    marker.write_text(f"123\n{int(time.time())}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gate, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(sys, "orig_argv", [sys.executable, "-m", "agent.transports.hermes_tools_mcp_server"])

    assert gate.should_quiesce_mcp_bridge() is True


def test_legacy_module_exits_cleanly_under_a_live_quiesce_marker(tmp_path):
    """The real ``-m agent...`` launch exits before ``agent`` preloads jiter."""
    (tmp_path / ".hermes-venv-quiesce").write_text(
        f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8"
    )
    env = {**os.environ, "HERMES_HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-X", "importtime", "-m", "agent.transports.hermes_tools_mcp_server"],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert "jiter.jiter" not in result.stderr
