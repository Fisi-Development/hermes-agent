"""Model-callable goal control at the real registry dispatch seam."""

import asyncio
import json
import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import goals
from model_tools import get_tool_definitions, handle_function_call


def _call(action: str, *, session_id: str, **kwargs):
    raw = handle_function_call(
        "goal_control",
        {"action": action, **kwargs},
        session_id=session_id,
        skip_pre_tool_call_hook=True,
        skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True,
    )
    return json.loads(raw)


def _home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    goals._DB_BOOTSTRAP_INFLIGHT.clear()
    return home


def test_set_returns_authoritative_persisted_current_session_state(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)

    result = _call(
        "set",
        session_id="session-current",
        condition="Run the focused checks",
        max_turns=7,
    )

    assert result["success"] is True
    assert result["session_id"] == "session-current"
    assert result["state"] == {
        "exists": True,
        "active": True,
        "paused": False,
        "status": "active",
        "condition": "Run the focused checks",
        "turns_used": 0,
        "max_turns": 7,
        "revision": 1,
        "stop_reason": None,
        "error_reason": None,
    }
    persisted = goals.load_goal("session-current")
    assert persisted is not None
    assert persisted.goal == "Run the focused checks"
    assert persisted.max_turns == 7


def test_goal_control_is_exposed_on_the_core_model_surface(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)

    definitions = get_tool_definitions(
        enabled_toolsets=["hermes-cli"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )

    names = {item["function"]["name"] for item in definitions}
    assert "goal_control" in names


def test_set_uses_configured_goal_budget_when_omitted(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    (home / "config.yaml").write_text("goals:\n  max_turns: 11\n", encoding="utf-8")

    result = _call("set", session_id="session-current", condition="Configured budget")

    assert result["success"] is True
    assert result["state"]["max_turns"] == 11


def test_status_requires_calling_session_identity():
    result = _call("status", session_id="")

    assert result == {
        "success": False,
        "session_id": None,
        "error": {
            "code": "missing_session_identity",
            "message": "calling session identity is required",
        },
    }


def test_cross_session_argument_is_rejected_without_mutation(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    raw = handle_function_call(
        "goal_control",
        {"action": "set", "condition": "wrong target", "session_id": "session-other"},
        session_id="session-current",
        skip_pre_tool_call_hook=True,
        skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True,
    )

    result = json.loads(raw)
    assert result["success"] is False
    assert result["error"]["code"] == "cross_session_forbidden"
    assert goals.load_goal("session-current") is None
    assert goals.load_goal("session-other") is None


def test_pause_resume_and_clear_return_persisted_state(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _call("set", session_id="session-current", condition="Keep going", max_turns=4)

    paused = _call("pause", session_id="session-current")
    assert paused["success"] is True
    assert paused["state"]["paused"] is True
    assert paused["state"]["status"] == "paused"
    assert paused["state"]["stop_reason"] == "model-paused"

    resumed = _call("resume", session_id="session-current")
    assert resumed["success"] is True
    assert resumed["state"]["active"] is True
    assert resumed["state"]["status"] == "active"
    assert resumed["state"]["turns_used"] == 0

    for _ in range(2):
        cleared = _call("clear", session_id="session-current")
        assert cleared["success"] is True
        assert cleared["state"]["status"] == "cleared"
        assert cleared["state"]["active"] is False

    persisted = goals.load_goal("session-current")
    assert persisted is not None
    assert persisted.status == "cleared"


def test_model_pause_updates_cached_cli_lifecycle_manager(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    from cli import HermesCLI

    session_id = "session-current"
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = session_id
    cli.agent = MagicMock(session_id=session_id)
    cli._pending_input = queue.Queue()
    cli._last_turn_interrupted = False
    cli.conversation_history = [
        {"role": "assistant", "content": "work completed this turn"}
    ]
    cli._goal_manager = goals.GoalManager(session_id=session_id, default_max_turns=4)
    cli._goal_manager.set("Keep going")

    paused = _call("pause", session_id=session_id)
    assert paused["success"] is True

    with patch("hermes_cli.goals.judge_goal") as judge:
        cli._maybe_continue_goal_after_turn()

    judge.assert_not_called()
    assert cli._pending_input.empty()
    assert cli._goal_manager.state.status == "paused"
    assert goals.load_goal(session_id).status == "paused"


def test_resume_does_not_reactivate_cleared_goal(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _call("set", session_id="session-current", condition="Terminal goal")
    _call("clear", session_id="session-current")

    resumed = _call("resume", session_id="session-current")

    assert resumed["success"] is False
    assert resumed["error"]["code"] == "invalid_transition"
    assert goals.load_goal("session-current").status == "cleared"


def test_pause_does_not_acknowledge_cleared_goal(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _call("set", session_id="session-current", condition="Terminal goal")
    _call("clear", session_id="session-current")

    paused = _call("pause", session_id="session-current")

    assert paused["success"] is False
    assert paused["error"]["code"] == "invalid_transition"
    assert goals.load_goal_authoritative("session-current").status == "cleared"


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_transition_race_reports_conflict_and_preserves_independent_clear(
    tmp_path, monkeypatch, action
):
    _home(tmp_path, monkeypatch)
    session_id = f"transition-race-{action}"
    _call("set", session_id=session_id, condition="Do not resurrect")
    if action == "resume":
        _call("pause", session_id=session_id)

    original = getattr(goals.GoalManager, action)

    def clear_before_transition(self, *args, **kwargs):
        goals.GoalManager(session_id).clear()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(goals.GoalManager, action, clear_before_transition)

    result = _call(action, session_id=session_id)

    assert result["success"] is False
    assert result["error"]["code"] == "transition_conflict"
    assert goals.load_goal_authoritative(session_id).status == "cleared"


@pytest.mark.parametrize("action", ["set", "update"])
def test_set_update_cas_conflict_cannot_resurrect_concurrently_cleared_goal(
    tmp_path, monkeypatch, action
):
    _home(tmp_path, monkeypatch)
    session_id = f"set-clear-race-{action}"
    _call("set", session_id=session_id, condition="original", max_turns=4)

    db = goals._get_session_db()
    real_cas = db.compare_and_set_meta
    competed = False

    def clear_before_first_cas(key, expected, replacement):
        nonlocal competed
        if not competed:
            competed = True
            current_raw = db.get_meta(key)
            cleared = goals.GoalState.from_json(current_raw)
            cleared.status = "cleared"
            cleared.revision += 1
            assert real_cas(key, current_raw, cleared.to_json()) is True
        return real_cas(key, expected, replacement)

    monkeypatch.setattr(db, "compare_and_set_meta", clear_before_first_cas)

    result = _call(
        action,
        session_id=session_id,
        condition="must not resurrect",
        max_turns=3,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "mutation_conflict"
    persisted = goals.load_goal_authoritative(session_id)
    assert persisted.status == "cleared"
    assert persisted.goal == "original"


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_pause_resume_is_bound_to_the_exact_eligible_snapshot(
    tmp_path, monkeypatch, action
):
    _home(tmp_path, monkeypatch)
    session_id = f"snapshot-bound-{action}"
    _call("set", session_id=session_id, condition="original", max_turns=4)
    if action == "resume":
        _call("pause", session_id=session_id)

    original = getattr(goals.GoalManager, action)

    def replace_before_transition(self, *args, **kwargs):
        replacement = goals.GoalManager(session_id)
        replacement.set("independent replacement", max_turns=2)
        if action == "resume":
            replacement.pause("independent pause")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(goals.GoalManager, action, replace_before_transition)

    result = _call(action, session_id=session_id)

    assert result["success"] is False
    assert result["error"]["code"] == "transition_conflict"
    persisted = goals.load_goal_authoritative(session_id)
    assert persisted.goal == "independent replacement"
    assert persisted.status == ("active" if action == "pause" else "paused")


def test_failed_cli_refresh_does_not_execute_stale_goal(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    from cli import HermesCLI

    session_id = "session-current"
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = session_id
    cli.agent = MagicMock(session_id=session_id)
    cli._pending_input = queue.Queue()
    cli._last_turn_interrupted = False
    cli.conversation_history = [
        {"role": "assistant", "content": "work completed this turn"}
    ]
    cli._goal_manager = goals.GoalManager(session_id=session_id, default_max_turns=4)
    cli._goal_manager.set("Keep going")

    paused = _call("pause", session_id=session_id)
    assert paused["success"] is True

    def _failed_readback(_session_id):
        raise RuntimeError("storage temporarily unavailable")

    monkeypatch.setattr(goals, "load_goal_authoritative", _failed_readback)
    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("continue", "more work", False, None, False),
    ) as judge:
        cli._maybe_continue_goal_after_turn()

    judge.assert_not_called()
    assert cli._pending_input.empty()
    assert goals.load_goal(session_id).status == "paused"


def test_update_replaces_condition_and_budget(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _call("set", session_id="session-current", condition="First", max_turns=2)

    updated = _call(
        "update",
        session_id="session-current",
        condition="Second",
        max_turns=9,
    )

    assert updated["success"] is True
    assert updated["state"]["condition"] == "Second"
    assert updated["state"]["max_turns"] == 9


def test_status_returns_stop_and_error_reasons_from_persisted_state(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    state = goals.GoalState(
        goal="Repair judge routing",
        status="paused",
        turns_used=3,
        max_turns=8,
        paused_reason="judge unavailable",
        last_reason="transport timeout",
        consecutive_transport_failures=2,
    )
    goals.save_goal("session-current", state)

    result = _call("status", session_id="session-current")

    assert result["success"] is True
    assert result["state"]["stop_reason"] == "judge unavailable"
    assert result["state"]["error_reason"] == "transport timeout"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("goal", "   "),
        ("status", "unknown"),
        ("revision", -1),
        ("turns_used", -1),
        ("max_turns", 0),
        ("consecutive_parse_failures", -1),
        ("consecutive_transport_failures", -1),
    ],
)
def test_semantically_invalid_persisted_goal_is_rejected(
    tmp_path, monkeypatch, field, value
):
    _home(tmp_path, monkeypatch)
    session_id = f"invalid-persisted-{field}"
    payload = json.loads(goals.GoalState(goal="valid goal").to_json())
    payload[field] = value
    goals._get_session_db().set_meta(
        goals._meta_key(session_id), json.dumps(payload, ensure_ascii=False)
    )

    with pytest.raises(goals.GoalPersistenceError, match="persisted goal state is invalid"):
        goals.load_goal_authoritative(session_id)

    result = _call("status", session_id=session_id)
    assert result["success"] is False
    assert result["error"]["code"] == "persistence_unavailable"


def test_failed_persistence_never_returns_success(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)

    class _DroppedWriteDB:
        def get_meta(self, _key):
            return None

        def set_meta(self, _key, _value):
            return None

    monkeypatch.setattr(goals, "_get_session_db", lambda: _DroppedWriteDB())

    result = _call("set", session_id="session-current", condition="Must persist")

    assert result["success"] is False
    assert result["error"]["code"] == "persistence_unavailable"


def test_dropped_budget_update_is_not_mistaken_for_success(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    raw = goals.GoalState(
        goal="Same condition",
        status="active",
        max_turns=2,
    ).to_json()

    class _DroppedUpdateDB:
        def get_meta(self, _key):
            return raw

        def set_meta(self, _key, _value):
            return None

    monkeypatch.setattr(goals, "_get_session_db", lambda: _DroppedUpdateDB())

    result = _call(
        "update",
        session_id="session-current",
        condition="Same condition",
        max_turns=9,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "persistence_unavailable"


@pytest.mark.parametrize("action", ["set", "update", "pause", "resume", "clear"])
def test_acknowledged_mutation_requires_exact_persisted_readback(
    tmp_path, monkeypatch, action
):
    _home(tmp_path, monkeypatch)
    session_id = f"exact-readback-{action}"
    if action != "set":
        _call("set", session_id=session_id, condition="original", max_turns=4)
    if action == "resume":
        _call("pause", session_id=session_id)

    db = goals._get_session_db()
    real_cas = db.compare_and_set_meta
    drifted = False

    def drift_after_commit(key, expected, replacement):
        nonlocal drifted
        committed = real_cas(key, expected, replacement)
        if committed and not drifted:
            drifted = True
            independent = goals.GoalState.from_json(replacement)
            independent.last_reason = "independent post-write change"
            independent.revision += 1
            assert real_cas(key, replacement, independent.to_json()) is True
        return committed

    monkeypatch.setattr(db, "compare_and_set_meta", drift_after_commit)
    kwargs = {"condition": "requested", "max_turns": 3} if action in {"set", "update"} else {}

    result = _call(action, session_id=session_id, **kwargs)

    assert result["success"] is False
    expected_code = "transition_conflict" if action in {"pause", "resume"} else "mutation_conflict"
    assert result["error"]["code"] == expected_code
    persisted = goals.load_goal_authoritative(session_id)
    assert persisted.last_reason == "independent post-write change"


def test_slow_initialization_fails_closed_instead_of_claiming_success(
    tmp_path, monkeypatch
):
    _home(tmp_path, monkeypatch)
    import hermes_state

    initialized = threading.Event()
    real_session_db = hermes_state.SessionDB

    class _SlowSessionDB(real_session_db):
        def __init__(self, *args, **kwargs):
            time.sleep(0.1)
            super().__init__(*args, **kwargs)
            initialized.set()

    monkeypatch.setattr(hermes_state, "SessionDB", _SlowSessionDB)
    monkeypatch.setattr(goals, "_DB_BOOTSTRAP_INIT_WAIT_S", 0.01)

    async def _on_runtime_loop():
        return _call("set", session_id="session-current", condition="Slow write")

    result = asyncio.run(_on_runtime_loop())

    assert result["success"] is False
    assert result["error"]["code"] == "persistence_unavailable"
    assert initialized.wait(2), "background SessionDB initialization did not finish"
    assert goals.load_goal("session-current") is None
