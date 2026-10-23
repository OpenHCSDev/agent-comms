

from agent_comms.bus import Bus
from agent_comms.field_codec import FieldCodec

"""External contracts and adding commands without editing any consumer catalog."""

import json

import pytest

from agent_comms.cli import build_parser, main
from agent_comms.cli_commands import CliCommand
from agent_comms.comms import wire
from agent_comms.native_arguments import ConfiguredBinary, GivenArguments, GivenBinary, InheritedArguments
from agent_comms.history_result import (
    ChannelHistoryPageResult,
    DmHistoryPageResult,
    EverythingHistoryPageResult,
)
from agent_comms.messages import InfoKind
from agent_comms.pi_vocabulary import ServiceTier
from agent_comms.runtime_requests import (
    RuntimeRequest,
)
from agent_comms.threads import Thread


def test_cli_decodes_tag_sets_enums_json_and_shell_words_once(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_COMMS_AGENT_BIN", "/a pi")
    monkeypatch.setenv("AGENT_COMMS_AGENT_ARGS", '--label "two words"')
    parser = build_parser()

    def parse(args):
        options = parser.parse_args(args)
        return CliCommand.member_named(options.command).from_arguments(options)

    register = parse(["register", "--name", "a", "--worktree", "/wt", "--tags", "x, y,x"])
    assert register.name == "a" and register.tags == frozenset({"x", "y"})
    assert parse(["inbox", "--thread", "a"]).thread == "a"
    send_arguments = ["user-send", "--to", "a", "--body", "hello"]
    monkeypatch.chdir(tmp_path)
    assert parse(send_arguments).worktree == str(tmp_path)
    changed = tmp_path / "changed"
    changed.mkdir()
    monkeypatch.chdir(changed)
    assert parse(send_arguments).worktree == str(changed)
    assert parse([*send_arguments, "--worktree", str(tmp_path)]).worktree == str(tmp_path)
    invoke = parse(["tool", "--tool", "comms_threads"])
    assert invoke.arguments == {}
    send = parse(["send", "--from", "a", "--to", "b", "--body", "hello"])
    assert send.type is InfoKind
    for tier in ServiceTier:
        configured = parse(["configure", "--name", "a", "--service-tier", tier.value])
        assert configured.service_tier.tier is tier
    with pytest.raises(ValueError) as refused:
        parser.parse_args(["configure", "--name", "a", "--service-tier", "fast"])
    assert "fast" in str(refused.value)
    assert all(tier.value in str(refused.value) for tier in ServiceTier)
    restart = parse(["restart", "--all"])
    assert restart.all_ is True
    assert restart.agent_bin == ConfiguredBinary()
    assert restart.agent_args == InheritedArguments()
    explicit = parse(
        ["restart", "--all", "--agent-bin", "/a pi", "--agent-args", '--label "two words"']
    )
    assert explicit.agent_bin == GivenBinary("/a pi")
    assert explicit.agent_args == GivenArguments(("--label", "two words"))
    monkeypatch.setenv("AGENT_COMMS_AGENT_BIN", "/next pi")
    fresh = build_parser().parse_args(["restart", "--all"])
    assert CliCommand.member_named(fresh.command).from_arguments(fresh).agent_bin == ConfiguredBinary()


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
    invalid = {**FieldCodec.encode(request), "futureExtension": {"yes": True}}
    with pytest.raises(ValueError) as declared:
        FieldCodec.decode(type(request), invalid)
    with pytest.raises(type(declared.value)) as refused:
        RuntimeRequest.from_wire({**payload, "futureExtension": {"yes": True}})
    assert str(refused.value) == str(declared.value)


@pytest.mark.parametrize(
    "parameters",
    [
        {"action": "input_dispositions", "include_history": "yes"},
        {"action": "goal_history", "goal_id": 2},
        {"action": "edit_goal", "goal_id": "g", "expected_revision": True, "text": "x"},
        {"action": "retry_goal"},
        {"action": "set_goal", "text": "  "},
        {"action": "edit_goal", "goal_id": "g", "expected_revision": 1, "text": False},
    ],
)
def test_invalid_runtime_parameters_use_canonical_decode_and_domain_errors(parameters):
    payload = {"thread": "owner", **parameters}
    values = dict(payload)
    action = values.pop("action")
    declaration = RuntimeRequest.member_named(action)
    with pytest.raises(ValueError) as declared:
        FieldCodec.decode(declaration, {"kind": action, **values})
    with pytest.raises(type(declared.value)) as refused:
        RuntimeRequest.from_wire(payload)
    assert str(refused.value) == str(declared.value)


@pytest.mark.parametrize(
    "arguments,result_type,per_root",
    [
        (["history", "#team"], ChannelHistoryPageResult, 1),
        (["history", "#any"], EverythingHistoryPageResult, 2),
        (["history", "alice", "--as", "bob"], DmHistoryPageResult, 1),
    ],
)
def test_normal_history_cli_keeps_migrated_content_and_source_identity(
    tmp_path, capsys, monkeypatch, arguments, result_type, per_root
):
    old, live = wire(tmp_path / "old"), wire(tmp_path / "live")
    for comms in (old, live):
        Bus.install(Bus.path_in(comms.root))
        for name in ("alice", "bob"):
            comms.registry.declare(
                Thread(name, frozenset({"team"}), str(tmp_path))
            )
        comms.messaging.send("alice", "#team", f"{comms.root.name} channel").message.message_id
        comms.messaging.send("alice", "bob", f"{comms.root.name} direct").message.message_id
    live.views.attach_history(old.root)
    live_bus = live.bus.bus.revision()
    assert main(["--root", str(live.root), *arguments]) == 0
    rows = json.loads(capsys.readouterr().out)[result_type.messages_key]
    historical = [row for row in rows if "history" in row]
    current = [row for row in rows if "history" not in row]
    assert len(historical) == len(current) == per_root
    assert all(row["text"].startswith("old ") for row in historical)
    assert all(row["history"]["source"] == str(old.root) for row in historical)
    assert all(
        row["history"]["sender_created_at"] == old.registry.require("alice").created_at
        for row in historical
    )
    assert live.bus.bus.revision() == live_bus
