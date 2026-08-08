"""Agent internals -- extracted modules from run_agent.py.

These modules contain pure utility functions and self-contained classes
that were previously embedded in the 3,600-line run_agent.py. Extracting
them makes run_agent.py focused on the AIAgent orchestrator class.
"""

# This check must precede jiter_preload.  The Codex MCP bridge is launched
# with ``python -m agent.transports...``; Python imports this package before
# the module entrypoint, and jiter_preload maps a native venv extension on
# Windows.  During a consented Desktop update, exit before that mapping.
from hermes_mcp_update_gate import should_quiesce_mcp_bridge

if should_quiesce_mcp_bridge():
    raise SystemExit(0)

from . import jiter_preload as _jiter_preload  # noqa: F401
