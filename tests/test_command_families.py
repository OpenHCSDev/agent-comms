
from agent_comms.field_codec import FieldCodec
"""External contracts and adding commands without editing any consumer catalog."""

import asyncio
import json
import os
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from agent_comms.cli import build_parser, main
from agent_comms.cli_commands import CliCommand
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.messages import InfoKind
from agent_comms.pi_vocabulary import ServiceTier
from agent_comms.owner_protocol import OwnerFailure, OwnerLine
from agent_comms.runtime import RuntimeProxy, RuntimeServer
from agent_comms.runtime_requests import (
    ResultRuntimeRequest,
    RuntimeRequest,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.threads import Thread


def test_cli_decodes_tag_sets_enums_json_and_shell_words_once(monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_BIN", "/a pi")
    monkeypatch.setenv("AGENT_COMMS_AGENT_ARGS", '--label "two words"')
    parser = build_parser()

    def parse(args):
        options = parser.parse_args(args)
        return CliCommand.member_named(options.command).from_arguments(options)

    register = parse(["register", "--name", "a", "--worktree", "/wt", "--tags", "x, y,x"])
    assert register.tags == frozenset({"x", "y"})
    invoke = parse(["tool", "--tool", "comms_threads"])
    assert invoke.arguments == {}
    send = parse(["send", "--from", "a", "--to", "b", "--body", "hello"])
    assert send.type is InfoKind
    for tier in ServiceTier:
        configured = parse(["configure", "--name", "a", "--service-tier", tier.value])
        assert configured.service_tier is tier
    with pytest.raises(ValueError, match="invalid choice: 'fast'"):
        parser.parse_args(["configure", "--name", "a", "--service-tier", "fast"])
    restart = parse(["restart", "--all"])
    assert restart.all_ is True
    assert restart.agent_bin is None
    assert restart.agent_args is None
    explicit = parse(["restart", "--all", "--agent-bin", "/a pi", "--agent-args", '--label "two words"'])
    assert explicit.agent_bin == "/a pi"
    assert explicit.agent_args == ["--label", "two words"]
    monkeypatch.setenv("AGENT_COMMS_AGENT_BIN", "/next pi")
    assert build_parser().parse_args(["restart", "--all"]).agent_bin is None


def test_one_cli_declaration_adds_parser_decode_and_real_dispatch(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(CliCommand, "__registry__", dict(CliCommand.__registry__))

    @dataclass(frozen=True)
    class ContractProbeResult(CliResult):
        amount: int
        root: str

    @dataclass(frozen=True, kw_only=True)
    class ContractProbeCliCommand(CliCommand, declared_name="contract-probe"):
        help = "Test extension through declaration membership"
        amount: int = FieldCodec.option("--amount")

        def apply(self, ctx):
            return ContractProbeResult(self.amount + 1, str(ctx.root))

    comms = Comms(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    assert main(["--root", str(tmp_path), "contract-probe", "--amount", "40"]) == 0
    assert json.loads(capsys.readouterr().out) == {"amount": 41, "root": str(tmp_path)}


@pytest.mark.parametrize(
    "parameters",
    [
        {"action": "subscribe"},
        {
            "action": "prompt",
            "prompt": [{"type": "text", "text": "hello"}],
            "meta": {},
            "controllerToken": "secret",
        },
        {"action": "cancel"},
        {"action": "set_config_option", "config_id": "model", "value": "test/model"},
        {"action": "compact", "instructions": "focus"},
        {"action": "input_dispositions", "include_history": True},
        {"action": "dismiss_historical_inputs"},
        {"action": "goal_history", "goal_id": "g"},
        {"action": "goal_snapshot"},
        {"action": "edit_goal", "goal_id": "g", "expected_revision": 3, "text": "new goal"},
        {"action": "update_goal", "goal_id": "g", "expected_revision": 3, "status": "paused"},
        {"action": "retry_goal", "goal_id": "g", "expected_revision": 3},
        {"action": "set_goal", "text": "new goal"},
    ],
)
def test_runtime_wire_format_round_trip(parameters):
    payload = {"thread": "alias", **parameters}
    request = RuntimeRequest.from_wire(payload)
    encoded = request.to_wire()
    assert {key: encoded[key] for key in payload} == payload
    assert "kind" not in encoded
    assert RuntimeRequest.from_wire(encoded) == request
    with pytest.raises(ValueError, match="Unknown fields.*futureExtension"):
        RuntimeRequest.from_wire({**payload, "futureExtension": {"yes": True}})


@pytest.mark.parametrize(
    "parameters,message",
    [
        (
            {"action": "input_dispositions", "include_history": "yes"},
            "Expected.*bool",
        ),
        ({"action": "goal_history", "goal_id": 2}, "Value does not match str"),
        (
            {"action": "edit_goal", "goal_id": "g", "expected_revision": True, "text": "x"},
            "Expected.*int",
        ),
        ({"action": "retry_goal"}, "Missing.*goal_id.*expected_revision"),
        ({"action": "set_goal", "text": "  "}, "A goal requires text."),
        (
            {"action": "edit_goal", "goal_id": "g", "expected_revision": 1, "text": False},
            "Expected.*str",
        ),
    ],
)
def test_invalid_runtime_parameters_use_canonical_decode_and_domain_errors(parameters, message):
    with pytest.raises((ValueError, TypeError), match=message):
        RuntimeRequest.from_wire({"thread": "owner", **parameters})


@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime")
async def test_one_runtime_declaration_works_through_proxy_and_actual_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(RuntimeRequest, "__registry__", dict(RuntimeRequest.__registry__))

    @dataclass(frozen=True, kw_only=True)
    class ContractProbeRuntimeRequest(ResultRuntimeRequest):
        amount: int

        async def result(self, ctx):
            return {"amount": self.amount + 1, "session": ctx.session_id, "owner": ctx.name}

    comms = Comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    def require_owned_session(entry):
        assert entry.thread.name == "owner"
        assert entry.matches_name("owner")
        return "session"

    async def awaiting_input_keys(session_id):
        assert session_id == "session"
        return frozenset()

    agent = SimpleNamespace(
        _comms=comms,
        inputs=SimpleNamespace(awaiting_input_keys=awaiting_input_keys),
        sessions=SimpleNamespace(
            bindings={"session": "owner"},
            require_owned_session=require_owned_session,
        ),
    )
    server = RuntimeServer(agent)
    await server.start()
    proxy = RuntimeProxy(agent, "owner", server.path)
    try:
        assert await proxy.request("contract_probe", amount=40) == {
            "amount": 41,
            "session": "session",
            "owner": "owner",
        }
        delivery = await proxy.request("input_dispositions")
        assert delivery["inputs"] == [] and delivery["historicalCount"] == 0
        with pytest.raises(RuntimeError, match="Expected.*int"):
            await proxy.request("contract_probe", amount=True)
        with pytest.raises(RuntimeError, match="Unknown fields.*futureExtension"):
            await proxy.request("contract_probe", amount=40, futureExtension=True)
        with pytest.raises(RuntimeError, match="Unknown runtime request field: kind"):
            await proxy.request("contract_probe", amount=40, kind="cancel")
        reader, writer = await asyncio.open_unix_connection(server.path)
        try:
            writer.write(b'{"action":"not-a-request","thread":"owner"}\n')
            await writer.drain()
            failure = OwnerLine.read(await reader.readline())
            assert isinstance(failure, OwnerFailure) and failure.rpc_error is None
            assert "Unknown RuntimeRequest" in failure.error
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await proxy.close()
        await server.close()


@pytest.mark.parametrize(
    "arguments,key",
    [
        (["history", "--channel", "#team"], "messages"),
        (["history", "--everything"], "everything"),
        (["history", "--with", "alice", "--as", "bob"], "dm"),
    ],
)
def test_normal_history_cli_keeps_migrated_content_and_source_identity(
    tmp_path, capsys, monkeypatch, arguments, key
):
    old, live = Comms(tmp_path / "old"), Comms(tmp_path / "live")
    for comms in (old, live):
        for name in ("alice", "bob"):
            comms.registry.declare(old.registry.new_thread(name, frozenset({'team'}), str(tmp_path)))
        comms.messaging.send("alice", "#team", f"{comms.root.name} channel").message.message_id
        comms.messaging.send("alice", "bob", f"{comms.root.name} direct").message.message_id
    source = live.views.attach_history(old.root)
    live_bus = (live.root / "bus.jsonl").read_bytes()
    assert main(["--root", str(live.root), *arguments]) == 0
    rows = json.loads(capsys.readouterr().out)[key]
    historical = [row for row in rows if "history" in row]
    current = [row for row in rows if "history" not in row]
    assert len(historical) == len(current) == (2 if key == "everything" else 1)
    assert all(row["text"].startswith("old ") for row in historical)
    assert all(row["history"]["source"] == str(old.root) for row in historical)
    assert all(row["history"]["wire_root_id"] == source.wire_root_id for row in historical)
    assert all(
        row["history"]["sender_created_at"] == old.registry.require("alice").created_at
        for row in historical
    )
    assert (live.root / "bus.jsonl").read_bytes() == live_bus
