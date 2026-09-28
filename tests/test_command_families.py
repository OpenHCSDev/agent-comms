"""External contracts and adding commands without editing any consumer catalog."""

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_comms.cli import build_parser, main
from agent_comms.cli_commands import CliCommand, option
from agent_comms.comms import wire
from agent_comms.messages import MessageType
from agent_comms.runtime import RuntimeProxy, RuntimeServer
from agent_comms.runtime_requests import (
    ResultRuntimeRequest,
    RuntimeRequest,
    SubscribeRuntimeRequest,
)
from agent_comms.threads import Thread


def test_all_cli_help_and_flags_match_before_refactor(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("AGENT_COMMS_AGENT_ARGS", raising=False)
    monkeypatch.delenv("AGENT_COMMS_AGENT_BIN", raising=False)
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    actual = {
        "help": parser.format_help(),
        "commands": {name: p.format_help() for name, p in sub.choices.items()},
    }
    expected = json.loads((Path(__file__).parent / "fixtures/cli-parser-contract.json").read_text())
    assert actual == expected


def test_cli_decodes_tag_sets_enums_json_and_shell_words_once(monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_BIN", "/a pi")
    monkeypatch.setenv("AGENT_COMMS_AGENT_ARGS", '--label "two words"')
    parser = build_parser()

    def parse(args):
        return CliCommand.from_namespace(parser.parse_args(args))

    register = parse(["register", "--name", "a", "--worktree", "/wt", "--tags", "x, y,x"])
    assert register.tags == frozenset({"x", "y"})
    invoke = parse(["invoke", "--tool", "comms_threads"])
    assert invoke.arguments == {}
    send = parse(["send", "--from", "a", "--to", "b", "--body", "hello"])
    assert send.type is MessageType.INFO
    restart = parse(["restart", "--all"])
    assert restart.all_ is True
    assert restart.agent_bin == "/a pi"
    assert restart.agent_args == ["--label", "two words"]
    monkeypatch.setenv("AGENT_COMMS_AGENT_BIN", "/next pi")
    assert build_parser().parse_args(["restart", "--all"]).agent_bin == "/next pi"


def test_one_cli_declaration_adds_parser_decode_and_real_dispatch(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(CliCommand, "__registry__", dict(CliCommand.__registry__))

    @dataclass(frozen=True, kw_only=True)
    class ContractProbeCliCommand(CliCommand, declared_name="contract-probe"):
        help = "Test extension through declaration membership"
        amount: int = option("--amount")

        def apply(self, ctx):
            return {"amount": self.amount + 1, "root": str(ctx.root)}

    assert main(["--root", str(tmp_path), "contract-probe", "--amount", "40"]) == 0
    assert json.loads(capsys.readouterr().out) == {"amount": 41, "root": str(tmp_path)}


@pytest.mark.parametrize(
    "parameters",
    [
        {"action": "subscribe", "transcriptSnapshots": True, "transcriptDiffs": False},
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
        ({"action": "retry_goal"}, "missing.*goal_id.*expected_revision"),
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

    comms = wire(tmp_path)
    comms.threads.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()))
    agent = SimpleNamespace(_comms=comms, sessions=SimpleNamespace(bindings={"session": "owner"}))
    server = RuntimeServer(agent)
    await server.start()
    proxy = RuntimeProxy(agent, "owner", server.path)
    try:
        assert await proxy.request("contract_probe", amount=40) == {
            "amount": 41,
            "session": "session",
            "owner": "owner",
        }
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
            result = json.loads(await reader.readline())
            assert set(result) == {"error"}
            assert "Unknown RuntimeRequest" in result["error"]
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await proxy.close()
        await server.close()


def test_subscribe_packet_keeps_external_camel_case_fields():
    assert SubscribeRuntimeRequest(thread="a", transcript_snapshots=True).to_wire() == {
        "action": "subscribe",
        "thread": "a",
        "transcriptSnapshots": True,
        "transcriptDiffs": False,
    }


@pytest.mark.parametrize(
    "arguments,key",
    [
        (["history", "--channel", "#team"], "messages"),
        (["history", "--everything"], "everything"),
        (["history", "--with", "alice", "--as", "bob"], "dm"),
    ],
)
def test_normal_history_cli_keeps_migrated_content_and_source_identity(
    tmp_path, capsys, arguments, key
):
    old, live = wire(tmp_path / "old"), wire(tmp_path / "live")
    for comms in (old, live):
        for name in ("alice", "bob"):
            comms.threads.register(Thread(name, frozenset({"team"}), str(tmp_path)))
        comms.messaging.send("alice", "#team", f"{comms.root.name} channel")
        comms.messaging.send("alice", "bob", f"{comms.root.name} direct")
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
