"""Inherited compact text formatting for MCP/model tool replies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import pytest

from agent_comms.comms import Comms
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import FieldCodec, MessageId, NullMember, WireExclude
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import ExternalThread, NativeThread
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
