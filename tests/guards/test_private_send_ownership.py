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
