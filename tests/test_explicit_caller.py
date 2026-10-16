"""Caller and project identity stay explicit across real CLI dispatch and resources."""
from contextlib import redirect_stdout
from io import StringIO
import json
import os

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.cli import main
from agent_comms.field_codec import FieldCodec
from agent_comms.launch_runtime import CallingThreadUnavailable
from agent_comms.errors import RelationViolationError
from agent_comms.native_project import NativeProject, ManagedNativeProject
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
    assert result["caller"] == FieldCodec.encode(OperatorCallerRecord(arguments=()))
    assert result["caller"]["arguments"] == []


def test_actual_cli_consumes_typed_identity_and_rejects_reused_name(comms, monkeypatch):
    owner = comms.registry.declare(Thread(name="owner", tags=frozenset(), worktree="/wt"))
    monkeypatch.setenv("AGENT_COMMS_THREAD", "wrong-owner")
    code, result = cli(comms, *owner.incarnation.cli_arguments, "tools")
    assert code == 0
    assert result["caller"]["kind"] == "thread"
    assert result["caller"]["name"] == owner.name
    assert result["caller"]["arguments"] == list(owner.incarnation.cli_arguments)
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


def test_acquisition_rejects_a_different_birth_without_taking_process_custody(comms, tmp_path):
    original = comms.registry.declare(Thread(name="original", tags=frozenset(), worktree=str(tmp_path)))
    stale = RecordedThreadIncarnation(original.name, original.created_at + 1)
    with pytest.raises(RelationViolationError, match="no longer current"):
        comms.owners.acquire_thread(stale, owner_pid=os.getpid())
    assert comms.registry.require(original.name).process_identity is None


def project_owner(comms, tmp_path):
    owner = comms.registry.declare(Thread(name="owner", tags=frozenset(), worktree=str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid())))
    (comms.root / "runtime").mkdir(mode=0o700)
    return owner, ManagedNativeProject.capture(comms.root, comms.registry.snapshot(), owner)


def test_direct_project_flags_hold_the_original_socket_address(comms, tmp_path):
    owner, project = project_owner(comms, tmp_path)
    with project.bound_arguments() as arguments:
        bound = NativeProject.from_arguments(arguments).require_managed()
        socket = bound.socket
        assert socket.path.resolve() == project.socket.path
        assert bound.request == project.request
        assert project.request.binding.owner.incarnation == owner.incarnation
        borrowed = socket.path.parent
        assert borrowed.exists()
    assert not borrowed.exists()


def test_native_catalog_decodes_original_argv_and_returns_owned_flag_names(comms, tmp_path):
    owner, project = project_owner(comms, tmp_path)
    with project.bound_arguments() as project_arguments:
        arguments = ("node", "cli.js", "--mode", "rpc", *owner.incarnation.cli_arguments, *project_arguments)
        code, result = cli(comms, "tools", "--native-arguments", json.dumps(arguments))
    assert code == 0
    assert result["native_flags"] == [owner.incarnation.value_flag, NativeProject.value_flag]
    assert result["caller"]["arguments"] == list(owner.incarnation.cli_arguments)
    assert result["project"]["kind"] == "managed"
    assert result["project"]["request"]["action"] == "project"
