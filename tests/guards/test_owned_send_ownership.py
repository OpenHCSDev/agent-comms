"""Ordinary input admission must remain independent of the orchestration runner."""

import ast
import inspect
from pathlib import Path

import pytest

from agent_comms.owned_send_admission import OwnedSendAdmission
from agent_comms.owned_turn import OwnedTurn
from agent_comms.turn_goal_permission import TurnGoalPermission
from agent_comms.turn_input_binding import TurnInputBinding
from agent_comms.turn_input_source import TurnInputSource

pytestmark = pytest.mark.refactor_guard


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
