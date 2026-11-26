"""Inherited compact text formatting for MCP/model tool replies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import pytest

from agent_comms.comms import Comms
from agent_comms.coordination.service_passes import ServicePassStore
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import InfoKind, Message
from agent_comms.nominal.field_codec import FieldCodec, MessageId, NullMember, WireExclude
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import ExternalThread, NativeThread
from agent_comms.team.team_hud import TeamHud, TeamLoad
from agent_comms.team.vibe_check import ChokeholdReleased
from agent_comms.transport.mcp_wire import AnsweredCall
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tool_request import ServedRequest
from agent_comms.transport.tool_result import ToolResult


@dataclass(frozen=True)
class _AbsentMarker(NullMember):
    pass


@dataclass(frozen=True)
class _SampleToolResult(ToolResult):
    title: str
    ref: MessageReference
    notes: tuple[str, ...] = ()
    empty: str = ""
    absent: _AbsentMarker = _AbsentMarker()
    hidden: Annotated[str, WireExclude()] = "secret"
    agent_comms_kind: str = "noise"


def test_inherited_compact_text_strips_noise_and_formats_nested_domain_values() -> None:
    ref = MessageReference(42, FieldCodec.decode(MessageId, "abcdef012345"))
    reply = _SampleToolResult(title="Ready", ref=ref, notes=("one", "two"))
    text = reply.compact_text()
    assert "title: Ready" in text
    assert "ref: 42-abcdef012345" in text
    assert "notes: one, two" in text
    assert "agentCommsKind" not in text and "agent_comms_kind" not in text
    assert "hidden" not in text and "secret" not in text
    assert "absent" not in text and "empty" not in text
    assert "{" not in text


def test_answered_call_keeps_structured_content_and_renders_compact_text(comms: Comms) -> None:
    comms.registry.declare(NativeThread("alpha", frozenset({"comms"}), Checkout(comms.root)))
    comms.registry.declare(ExternalThread("beta", frozenset({"comms"}), Checkout(comms.root)))
    comms.channels.create_tag("comms")
    caller = ThreadCaller("alpha")

    send_req = ServedRequest.member_named("comms_send").decoded(
        comms, {"to": "#comms", "body": "line one\nline two — detailed body"},
    )
    reply, board, text = send_req.answered_parts(comms, caller)
    mcp_result = AnsweredCall(reply, board, text).result()
    assert mcp_result.structured_content == reply
    assert mcp_result.content[0].text.startswith("Sent ")
    assert "#comms" in mcp_result.content[0].text
    assert "detailed body" not in mcp_result.content[0].text
    assert "agentCommsKind" not in mcp_result.content[0].text

    hist_req = ServedRequest.member_named("comms_history").decoded(
        comms, {"peer": "#comms", "limit": 10},
    )
    h_reply, h_board, h_text = hist_req.answered_parts(comms, caller)
    h_mcp = AnsweredCall(h_reply, h_board, h_text).result()
    assert h_mcp.structured_content == h_reply
    assert "#comms (1 msgs)" in h_mcp.content[0].text
    assert "line one\nline two — detailed body" in h_mcp.content[0].text
    assert '\\n' not in h_mcp.content[0].text

    threads_req = ServedRequest.member_named("comms_threads").decoded(comms, {})
    t_reply, t_board, t_text = threads_req.answered_parts(comms, caller)
    assert len(t_text) < len(FieldCodec.compact(t_reply))
    assert "alpha" in t_text and "beta" in t_text

    channels_req = ServedRequest.member_named("comms_channels").decoded(comms, {})
    c_reply, c_board, c_text = channels_req.answered_parts(comms, caller)
    assert "#comms (2:" in c_text and "alpha" in c_text and "beta" in c_text

    inbox_req = ServedRequest.member_named("comms_inbox").decoded(comms, {"thread": "beta"})
    i_reply, i_board, i_text = inbox_req.answered_parts(comms, caller)
    assert "Inbox (1 msgs)" in i_text
    assert "line one\nline two — detailed body" in i_text


def test_comms_board_renders_live_board_when_no_heartbeat_frame_exists(comms: Comms) -> None:
    """External-watch fleets have no native Pi heartbeat writing team_frames.json;
    comms_board must still return an accurate live board instead of BoardUnavailable."""
    comms.registry.declare(NativeThread("alpha", frozenset({"comms"}), Checkout(comms.root)))
    caller = ThreadCaller("alpha")
    goal_req = ServedRequest.member_named("comms_set_goal").decoded(
        comms, {"text": "Keep the board always live"},
    )
    goal_req.answered_parts(comms, caller)

    board_req = ServedRequest.member_named("comms_board").decoded(comms, {})
    b_reply, _, b_text = board_req.answered_parts(comms, caller)
    assert "Installed:" in b_text
    assert "Active teammates" in b_text
    assert "Open problems: none" in b_text
    assert "Tristan last completed" not in b_text
    assert b_reply["lines"] == list(b_text.splitlines())

    num_req = ServedRequest.member_named("comms_problem_number").decoded(comms, {})
    _, _, n_text = num_req.answered_parts(comms, caller)
    assert n_text.startswith("Reserved problem P")

    list_req = ServedRequest.member_named("comms_todo_list").decoded(comms, {})
    _, _, l_text = list_req.answered_parts(comms, caller)
    assert l_text == "No open problems"

    hud_req = ServedRequest.member_named("comms_hud").decoded(comms, {})
    h_reply, _, h_text = hud_req.answered_parts(comms, caller)
    assert "Live bus snapshot:" in h_text
    assert "Nothing is refreshing these rows" not in h_text
    assert "Tristan last completed" not in h_text
    assert "Codex usage unreadable" not in h_text
    assert "Heartbeat: live" in h_text
    assert "Load: 0" in h_text
    assert "IN PROGRESS · 1" in h_text and "alpha · Moving" in h_text
    assert h_reply["teammate"]["name"] == "alpha"
    assert FieldCodec.decode(TeamHud, h_reply).teammate.name == "alpha"

    load_req = ServedRequest.member_named("comms_team_load").decoded(comms, {})
    load_reply, _, load_text = load_req.answered_parts(comms, caller)
    assert "Live bus snapshot:" in load_text
    assert "Nothing is refreshing these rows" not in load_text
    assert "Tristan last completed" not in load_text
    assert "load 0" in load_text
    assert load_reply["load"]["owed"] == 0
    assert FieldCodec.decode(TeamLoad, FieldCodec.encode(TeamLoad.live(comms))).load.owed == 0

    rules_req = ServedRequest.member_named("comms_standing_rules").decoded(comms, {})
    _, _, rules_text = rules_req.answered_parts(comms, caller)
    assert "Load: 0" in rules_text

    # Real service failures still surface loud on live board, HUD, and load reads.
    ServicePassStore.at(comms.root).failed("TeamHeartbeat", RuntimeError("boom"))
    _, _, failed_board = board_req.answered_parts(comms, caller)
    _, _, failed_hud = hud_req.answered_parts(comms, caller)
    _, _, failed_load = load_req.answered_parts(comms, caller)
    for rendered in (failed_board, failed_hud, failed_load):
        assert "RuntimeError: boom" in rendered


def test_chokehold_released_without_pattern_catalog_recitation(comms: Comms) -> None:
    thread = comms.registry.declare(NativeThread("alpha", frozenset({"comms"}), Checkout(comms.root)))
    reply = Message("alpha", "user", "Fixed the stuck build directly.", InfoKind, timestamp=100.0, seq=1)
    assert len(ChokeholdReleased.judged(thread.incarnation, reply, ())) == 1
