"""Keep the retired evidence/authority/path mechanisms out of current source."""

import ast
import inspect
from pathlib import Path
from typing import get_type_hints

import pytest

from agent_comms import claim_admission, coordination_response
from agent_comms.envelope_claim_transitions import FileClaimPath
from agent_comms.messaging import Messaging
from agent_comms.publisher import Publisher

pytestmark = pytest.mark.refactor_guard

def test_replaced_pf_interfaces_cannot_return():
    retired = {
        "_read_native_context_evidence",
        "_require_live_registry_owner",
        "normalize_existing_file",
        "normalize_claim_file",
    }
    source = Path(__file__).parents[1] / "src" / "agent_comms"
    violations = []
    for path in source.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
            elif isinstance(node, ast.alias):
                name = node.name
            if (
                name in retired
                or (path.name == "private_bus_checkpoint.py" and name == "_failure")
                or (path.name == "channel_coding_tools.py" and name == "resource_claim")
            ):
                violations.append(f"{path.name}:{node.lineno}: {name}")
    assert not violations, "\n".join(violations)


def test_response_authority_cannot_restore_an_optional_or_pid_only_path():
    for operation in (
        coordination_response.prepare_fenced_response,
        coordination_response.publish_fenced_response,
        coordination_response.resolve_existing_response,
    ):
        parameters = inspect.signature(operation).parameters
        assert "owner_pid" not in parameters
        witness = parameters["owner_witness"]
        assert witness.default is inspect.Parameter.empty
        assert witness.annotation == "LiveResponseOwner"


def test_claim_admission_uses_nominal_paths_without_raw_coercion():
    assert get_type_hints(FileClaimPath)["resource"] is Path
    assert (
        inspect.signature(claim_admission.publish_selected_resource_claim)
        .parameters["resource_path"]
        .annotation
        == "FileClaimPath"
    )
    for operation in (Messaging.send, Messaging.send_message, Publisher.publish_claim_envelope):
        assert (
            inspect.signature(operation).parameters["claims"].annotation
            == "Sequence[FileClaimPath]"
        )
