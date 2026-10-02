"""Ordinary input admission must remain independent of the orchestration runner."""

import ast
import inspect
from pathlib import Path

import pytest

from agent_comms.owned_send_admission import OwnedSendAdmission
from agent_comms.owned_turn import OwnedTurn
from agent_comms.turn_goal_permission import TurnGoalPermission
from agent_comms.turn_input_binding import TurnInputBinding
from agent_comms.turn_input_source import OriginalTurnInput, TurnInputSource

pytestmark = pytest.mark.refactor_guard


def test_original_input_cases_inherit_reservation_and_compaction():
    """New input cases cannot silently opt out of the shared original path."""
    cases = list(OriginalTurnInput.__subclasses__())
    while cases:
        case = cases.pop()
        cases.extend(case.__subclasses__())
        assert case.reserve is OriginalTurnInput.reserve, case
        assert case.compaction_keys is OriginalTurnInput.compaction_keys, case


def test_owned_turn_no_longer_owns_send_authority_or_native_binding():
    tree = ast.parse(inspect.getsource(OwnedTurn))
    methods = {
        n.name for n in tree.body[0].body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not {"send_boundary", "native_start", "input_keys_valid"}.intersection(methods)
    assert not any(
        isinstance(n, ast.Attribute) and n.attr == "consume_bound_original" for n in ast.walk(tree)
    )


def test_admission_components_own_state_without_capturing_runner():
    for path in {
        Path(inspect.getfile(owner))
        for owner in (OwnedSendAdmission, TurnGoalPermission, TurnInputBinding, TurnInputSource)
    }:
        source = path.read_text()
        assert len(source.splitlines()) <= 1000, path
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.end_lineno - node.lineno + 1 <= 100, (path, node.name)
            if isinstance(node, ast.Name):
                assert node.id not in {"OwnedTurn", "TurnRunner"}, (path, node.lineno)
            if isinstance(node, ast.Attribute):
                assert node.attr != "runner", (path, node.lineno)


def test_ordinary_admission_preserves_named_rule_dispatch_until_callback():
    tree = ast.parse(inspect.getsource(OwnedSendAdmission))
    assert not any(
        isinstance(node, ast.FunctionDef) and node.name in {"_current_owner", "_accepted"}
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.BoolOp) and len(node.values) >= 4 for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Name) and node.id in {"owner_ok", "allowed"} for node in ast.walk(tree)
    )


def test_new_input_owners_do_not_restore_long_boolean_chains():
    for owner in (TurnInputSource, TurnInputBinding):
        path = Path(inspect.getfile(owner))
        assert not any(
            isinstance(node, ast.BoolOp) and len(node.values) >= 4
            for node in ast.walk(ast.parse(path.read_text()))
        ), path


def test_input_drain_does_not_restore_parallel_admission_maps():
    from agent_comms.input_drain import InputDrain
    from agent_comms.turn_runner import TurnRunner
    from agent_comms.agent_event_updates import AcpEventConsumer

    removed = {
        "forwarded_inputs",
        "steering_input_keys",
        "steering_goal_ids",
        "turn_original_input_keys",
        "turn_input_text",
    }
    for owner in (InputDrain, TurnRunner, OwnedTurn, OwnedSendAdmission, AcpEventConsumer):
        tree = ast.parse(inspect.getsource(owner))
        assert not any(
            isinstance(node, ast.Attribute) and node.attr in removed for node in ast.walk(tree)
        ), owner


def test_wake_dispatch_and_watch_modes_do_not_return_to_input_drain():
    from agent_comms.input_drain import InputDrain

    tree = ast.parse(inspect.getsource(InputDrain))
    methods = [
        node
        for node in tree.body[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert "schedule_wake" not in {node.name for node in methods}
    for method in methods:
        if method.name in {"ensure_live_drain", "observe"}:
            assert not any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                for child in method.body
                for node in ast.walk(child)
            )
    assert not any(
        isinstance(node, ast.Name)
        and node.id in {"open_wire_watcher", "PollingWireWatch", "WireChangeWatch"}
        for node in ast.walk(tree)
    )
