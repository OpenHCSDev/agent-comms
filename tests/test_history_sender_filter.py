"""Tests for 1-call owner/human & sender history filtering and fast StorageFloor reads."""

from __future__ import annotations

import json
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.messaging.channel_targets import ChannelTarget
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.storage_floor import StorageFloor
from agent_comms.registry.threads import ExternalThread, NativeThread
from agent_comms.resources.limits import GIB, HomeDiskLimit
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tool_request import ServedRequest


def test_comms_history_sender_filter_returns_human_owner_and_named_sender_turns(
    comms: Comms,
) -> None:
    comms.messaging.user_identity(Checkout(comms.root))
    comms.registry.declare(NativeThread("trainer", frozenset({"model"}), Checkout(comms.root)))
    comms.registry.declare(ExternalThread("eval", frozenset({"model"}), Checkout(comms.root)))
    comms.channels.create_tag("model")

    worktree = Checkout(comms.root)
    comms.messaging.send_user_message(ChannelTarget("#model"), "Directive 1: keep 4B default", worktree=worktree)
    for idx in range(5):
        comms.messaging.send("trainer", "#model", f"agent chatter {idx}")
    comms.messaging.send_user_message(ChannelTarget("#model"), "Directive 2: keep SFT adapter", worktree=worktree)
    comms.messaging.send("eval", "#model", "eval summary")
    comms.messaging.send_user_message(ChannelTarget("#model"), "Directive 3: native video required", worktree=worktree)

    caller = ThreadCaller("eval")
    tool = ServedRequest.member_named("comms_history")

    # 1-call human/owner verbatim pull skips all agent chatter.
    for selector in ("human", "owner", "user"):
        req = tool.decoded(comms, {"peer": "#model", "sender": selector, "limit": 10})
        reply, _, text = req.answered_parts(comms, caller)
        bodies = [m["text"] for m in reply["messages"]]
        assert bodies == [
            "Directive 3: native video required",
            "Directive 2: keep SFT adapter",
            "Directive 1: keep 4B default",
        ]
        assert "Directive 1: keep 4B default" in text
        assert "agent chatter" not in text

    # Pagination within filtered history works cleanly.
    page1_req = tool.decoded(comms, {"peer": "#model", "sender": "human", "limit": 2})
    p1_reply, _, _ = page1_req.answered_parts(comms, caller)
    assert [m["text"] for m in p1_reply["messages"]] == [
        "Directive 3: native video required",
        "Directive 2: keep SFT adapter",
    ]
    assert p1_reply["has_older"] is True

    page2_req = tool.decoded(
        comms, {"peer": "#model", "sender": "human", "limit": 2, "before": p1_reply["before"]},
    )
    p2_reply, _, _ = page2_req.answered_parts(comms, caller)
    assert [m["text"] for m in p2_reply["messages"]] == ["Directive 1: keep 4B default"]
    assert p2_reply["has_older"] is False

    # Named agent filter returns only that agent's turns.
    eval_req = tool.decoded(comms, {"peer": "#model", "sender": "eval", "limit": 10})
    e_reply, _, _ = eval_req.answered_parts(comms, caller)
    assert [m["text"] for m in e_reply["messages"]] == ["eval summary"]


def test_storage_floor_reads_storage_json_without_heavy_imports(tmp_path: Path) -> None:
    assert StorageFloor.read_floor_gib(tmp_path) == 10.0
    assert HomeDiskLimit(root=tmp_path).critical_bytes == 10 * GIB
    (tmp_path / "storage.json").write_text(json.dumps({"floor_gib": 25.0}))
    assert StorageFloor.read_floor_gib(tmp_path) == 25.0
    assert HomeDiskLimit(root=tmp_path).critical_bytes == 25 * GIB
