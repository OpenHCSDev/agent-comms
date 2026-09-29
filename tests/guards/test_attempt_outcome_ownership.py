"""Attempt outcome and replay logic must not grow a second store-owned path."""

import ast
import inspect
from pathlib import Path

import pytest

from agent_comms.attempt_store import AttemptStore
from agent_comms.goal_attempts import GoalAttemptStore

pytestmark = pytest.mark.refactor_guard


def test_stores_do_not_restore_outcome_or_row_transition_facades():
    removed = {
        "record_failed",
        "record_verified_progress",
        "record_verified_completion",
        "record_provider_usage",
        "provider_usage_total",
        "_generation",
        "_attempt",
        "_advance_generation",
        "_advance_attempt",
        "_is_attempt",
        "_is_attempt_phase",
        "_require_current",
        "settle_checked",
        "advance_checked",
        "record_replay",
    }
    for owner in (GoalAttemptStore, AttemptStore):
        tree = ast.parse(inspect.getsource(owner))
        declared = {node.name for node in tree.body[0].body if isinstance(node, ast.FunctionDef)}
        assert not declared & removed, owner


def test_goal_admission_uses_claimed_permit_without_removed_store_probe():
    from agent_comms.ordinary_admission_rules import OrdinaryGoalGrantRule

    source = Path(inspect.getfile(OrdinaryGoalGrantRule)).read_text()
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "_is_attempt"
        for node in ast.walk(ast.parse(source))
    )
