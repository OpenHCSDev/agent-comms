"""Live observations own state; orchestration cannot regain a shared-self facade."""

import ast
import inspect
from pathlib import Path

import pytest

from agent_comms.channel_input_batch import InputBatch
from agent_comms.owned_turn import OwnedTurn
from agent_comms.turn_goal_account import TurnGoalAccount
from agent_comms.turn_progress import TurnEventPublication, TurnProgress

pytestmark = pytest.mark.refactor_guard


def test_progress_and_goal_accounts_never_capture_execution_or_runner():
    for owner in (TurnProgress, TurnEventPublication, TurnGoalAccount):
        tree = ast.parse(inspect.getsource(owner))
        assert not any(
            isinstance(node, ast.Attribute) and node.attr in {"execution", "runner"}
            for node in ast.walk(tree)
        )
    assert not hasattr(TurnProgress, "finish_attempts")


def test_turn_owners_have_bounded_methods_and_no_new_long_predicates():
    for path in {
        Path(inspect.getfile(c)) for c in (InputBatch, OwnedTurn, TurnGoalAccount, TurnProgress)
    }:
        source = path.read_text()
        assert len(source.splitlines()) <= 1000
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.end_lineno - node.lineno + 1 <= 100, (path, node.name)
            if isinstance(node, ast.BoolOp):
                assert len(node.values) < 4, (path, node.lineno)
