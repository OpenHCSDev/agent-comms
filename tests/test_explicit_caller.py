"""Caller identity stays explicit across actual CLI dispatch and native arguments."""
from contextlib import redirect_stdout
from io import StringIO
import json

import pytest

from agent_comms.cli import main
from agent_comms.field_codec import FieldCodec
from agent_comms.launch_runtime import CallingThreadUnavailable
from agent_comms.runtime_requests import ProjectRuntimeRequest
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.threads import Thread
from agent_comms.tool_caller import NoThreadCaller, OperatorCallerRecord, ThreadCaller


def cli(comms, *arguments):
    output = StringIO()
    with redirect_stdout(output):
        code = main(["--root", str(comms.root), *arguments])
    return code, json.loads(output.getvalue())


def test_cli_catalog_has_explicit_operator_not_environment_owner(comms, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_THREAD", "invented-owner")
    code, result = cli(comms, "tools")
    assert code == 0
    assert result["caller"] == FieldCodec.encode(OperatorCallerRecord())
    assert result["caller_flag"] == RecordedThreadIncarnation.value_flag


def test_actual_cli_consumes_typed_identity_and_rejects_reused_name(comms, monkeypatch):
    owner = comms.registry.declare(Thread(name="owner", tags=frozenset(), worktree="/wt"))
    monkeypatch.setenv("AGENT_COMMS_THREAD", "wrong-owner")
    code, result = cli(comms, *owner.incarnation.cli_arguments, "tools")
    assert code == 0
    assert result["caller"]["kind"] == "thread"
    assert result["caller"]["name"] == owner.name
    changed = RecordedThreadIncarnation(owner.name, owner.created_at + 1)
    code, result = cli(comms, *changed.cli_arguments, "invoke", "--tool", "comms_collaborations", "--arguments", "{}")
    assert code == 1
    assert "no longer current" in result["error"]


def test_caller_relative_subject_requires_an_explicit_owner(comms):
    owner = comms.registry.declare(Thread(name="owner", tags=frozenset(), worktree="/wt"))
    assert NoThreadCaller().subject("named") == "named"
    with pytest.raises(CallingThreadUnavailable):
        NoThreadCaller().subject("")
    assert ThreadCaller(owner.incarnation).subject("") == owner.name


def test_project_flags_preserve_original_request_and_consume_capture():
    request = '{"action":"project","thread":"original"}'
    captured = {"AGENT_COMMS_PROJECT_SOCKET": "/original/owner.sock", "AGENT_COMMS_PROJECT_REQUEST": request}
    assert ProjectRuntimeRequest.take_native_arguments(captured) == (
        ProjectRuntimeRequest.socket_flag, "/original/owner.sock", ProjectRuntimeRequest.request_flag, request,
    )
    assert captured == {}
    with pytest.raises(ValueError, match="incomplete"):
        ProjectRuntimeRequest.take_native_arguments({"AGENT_COMMS_PROJECT_SOCKET": "/missing-request.sock"})
