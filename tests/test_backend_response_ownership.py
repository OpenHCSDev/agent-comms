"""Command extension and guards for the retired response/settlement replicas."""

import ast
from pathlib import Path

import pytest

from agent_comms import pi_commands as commands
from agent_comms.backend import TurnSession
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.pi_payloads import StateData
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.turn_stats import StatsRequest


class InspectionSnapshot(commands.SessionSnapshot, commands.PiCommand):
    response_payload = StateData


class InspectionFork(commands.MutatesSession, commands.PiCommand):
    pass


@pytest.mark.parametrize("success", [True, False])
def test_new_command_capabilities_guard_identity_without_backend_dispatch_edits(success):
    session = TurnSession(
        NativePiRpcLaunch(("unused",), Path.cwd(), {}, Path.cwd(), None, Path.cwd()), "unused"
    )
    session.initial_session_observed = True
    session.initial_session_id = "selected"
    session.initial_session_file = "/selected.jsonl"
    import json

    def response(owner, identity, path):
        return PiRpcChannel.decode_record(
            json.dumps(
                {
                    "type": "response",
                    "command": owner.declared_name,
                    "success": success,
                    "data": {"sessionId": identity, "sessionFile": path},
                }
            ).encode()
        )

    for identity, path in (("foreign", "/selected.jsonl"), ("selected", "/foreign.jsonl")):
        record = response(InspectionSnapshot, identity, path)
        assert record.command.invalidates_identity(record, session) is success
    record = response(InspectionSnapshot, "selected", "/selected.jsonl")
    assert not record.command.invalidates_identity(record, session)
    # Even a failed mutating response cannot prove the session stayed unchanged.
    record = response(InspectionFork, "selected", "/selected.jsonl")
    assert record.command.invalidates_identity(record, session)


def test_backend_response_switches_and_stats_correlation_replica_stay_deleted():
    root = Path(__file__).parents[1] / "src/agent_comms"
    backend = ast.parse((root / "backend.py").read_text())
    retired = {"guard_identity", "observe_progress", "settle_or_continue"}
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in retired
        for node in ast.walk(backend)
    )
    assert {
        "state_id",
        "usage_id",
        "responses",
        "complete",
        "busy",
        "failed",
        "requested",
    }.isdisjoint(StatsRequest.__dataclass_fields__)
