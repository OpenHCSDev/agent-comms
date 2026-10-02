"""The irreversible private send belongs to a one-use owner, not a runner closure."""

import ast
import inspect
from pathlib import Path

import pytest

from agent_comms.native_input_owner import ParticipantOwner, RegistryOwner
from agent_comms.private_send_admission import PrivateSendAdmission
from agent_comms.private_send_stage import NativeSendStage

pytestmark = pytest.mark.refactor_guard
SOURCE = Path(__file__).resolve().parents[2] / "src" / "agent_comms"


def test_retired_private_send_closure_and_owner_helpers_have_no_callers():
    retired = {"_require_registry_owner", "_require_owner"}
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Name, ast.alias)):
                name = node.id if isinstance(node, ast.Name) else node.name
                assert name not in retired, (path, name)
            if isinstance(node, ast.ClassDef) and node.name == "SelectedExecution":
                assert all(getattr(child, "name", None) != "_send_boundary" for child in node.body)
    binding = ast.parse((SOURCE / "native_prompt_binding.py").read_text())
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "coordinated_runtime"
        for node in ast.walk(binding)
    )


def test_private_admission_owners_stay_within_s7_bounds_without_runner_state():
    for path in {
        Path(inspect.getfile(owner))
        for owner in (PrivateSendAdmission, NativeSendStage, RegistryOwner, ParticipantOwner)
    }:
        source = path.read_text()
        assert len(source.splitlines()) <= 1000, path
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.end_lineno - node.lineno + 1 <= 100, (path, node.name)
            if isinstance(node, ast.Name):
                assert node.id != "SelectedExecution", (path, node.lineno)


def test_private_send_does_not_rebuild_identity_tuples_or_compare_bare_pids():
    for name in ("native_input_owner", "private_send_stage", "native_admission_rules"):
        for node in ast.walk(ast.parse((SOURCE / f"{name}.py").read_text())):
            if isinstance(node, ast.Compare):
                assert not any(
                    isinstance(value, ast.Tuple) and len(value.elts) > 2
                    for value in (node.left, *node.comparators)
                ), (name, node.lineno)
                assert not any(
                    isinstance(value, ast.Attribute) and value.attr == "pid"
                    for value in ast.walk(node)
                ), (name, node.lineno)


def test_selected_lifetime_cannot_reintroduce_partial_runner_authority():
    """Reserved input/attempt authority must not become nullable runner scratch again."""
    from dataclasses import fields

    from agent_comms.coordinated_runtime import SelectedExecution

    retired = {
        "native_input",
        "progress",
        "owned_turn_lease",
        "assignment",
        "execution_id",
        "selected_write_plan_loader",
        "selected_write_plan_check",
        "selected_write_plan_applied",
    }
    assert retired.isdisjoint(field.name for field in fields(SelectedExecution))
    tree = ast.parse(inspect.getsource(SelectedExecution))
    writes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }
    assert retired.isdisjoint(writes)
    assert writes <= {field.name for field in fields(SelectedExecution)}
    for module in ("selected_participant", "selected_session", "selected_request", "selected_turn"):
        body = ast.parse((SOURCE / f"{module}.py").read_text())
        assert not any(
            isinstance(node, ast.Name) and node.id == "SelectedExecution" for node in ast.walk(body)
        ), module
        assert not any(
            isinstance(node, ast.ImportFrom) and node.module == "coordinated_runtime"
            for node in ast.walk(body)
        ), module


def test_native_execution_consumers_cannot_rebuild_stage_or_nullable_identity():
    """The admitted execution family replaces the repeated original raw tuple."""
    from dataclasses import fields
    from agent_comms.native_input_record import NativeInputIdentity

    assert {item.name for item in fields(NativeInputIdentity)} == {
        "input_id", "execution", "owner",
    }
    for module in (
        "private_send_stage", "historical_native_inputs", "source_proof_requirement",
        "selected_turn", "selected_tool_broker", "attempt_recovery",
    ):
        tree = ast.parse((SOURCE / f"{module}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                assert not (
                    any(isinstance(term, ast.Attribute) and term.attr in {"stage", "triage_result"}
                        for term in (node.left, *node.comparators))
                    and any(isinstance(term, ast.Constant) and term.value in {"triage", "full", "ignore"}
                            for term in (node.left, *node.comparators))
                ), (module, node.lineno)


def test_recorded_native_triage_decision_cannot_be_nullable_domain_state():
    from dataclasses import MISSING, fields
    from typing import get_type_hints
    from agent_comms.historical_native_inputs import (
        TriageHistoricalNativeInput, FullHistoricalNativeInput,
    )
    from agent_comms.selected_triage import SelectedTriage

    decision = next(item for item in fields(TriageHistoricalNativeInput) if item.name == "decision")
    assert decision.default is MISSING and decision.default_factory is MISSING
    assert get_type_hints(TriageHistoricalNativeInput)["decision"] == type[SelectedTriage]
    assert {item.name for item in fields(FullHistoricalNativeInput)}.isdisjoint({"decision", "triage_result"})
    for name in ("historical_native_inputs", "native_input_record"):
        tree = ast.parse((SOURCE / f"{name}.py").read_text())
        assert not any(isinstance(node, (ast.Name, ast.Attribute))
                       and (node.id if isinstance(node, ast.Name) else node.attr)
                       in {"triage_result", "require_triage_decision"}
                       for node in ast.walk(tree)), name
