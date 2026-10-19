"""Ordinary ACP input commits a native write claim through the real publisher."""

import asyncio
import json

from acp.schema import TextContentBlock

from agent_comms.channel_coding_tools import CodingToolOwner
from agent_comms.owned_send import OwnedSend
from agent_comms.messages import ClaimNoticeKind
from agent_comms.field_codec import FieldCodec
from agent_comms.storage_rules import StorageRule


async def test_ordinary_native_write_commits_claim(native_backend, monkeypatch):
    native = native_backend
    # The private project owns its fixture scratch, with the real storage checks enabled.
    (native.root / "storage.json").write_text(json.dumps(FieldCodec.encode(
        StorageRule(scratch_base=native.project.parent),
    )))
    await native.author_history()
    original = native.session.read_bytes()
    target = native.project / "ordinary-claimed.txt"
    claims = []
    allow = CodingToolOwner.allow

    async def observed_allow(owner, turn, round_number, call):
        assert isinstance(owner, OwnedSend), "This must exercise ordinary, not selected, input"
        await allow(owner, turn, round_number, call)
        claims.append(owner.claims[str(target.resolve())])

    def chunks():
        assert native.provider.posts <= 2, "Never replay the ordinary input or tool"
        if native.provider.posts == 1:
            yield {"tool_calls": [{
                "index": 0, "id": "ordinary-write", "type": "function",
                "function": {"name": "write", "arguments": json.dumps({
                    "path": str(target), "content": "P620 ordinary native write\n",
                })},
            }]}, None
            yield {}, "tool_calls"
        else:
            yield {"content": "P620 write complete"}, None
            yield {}, "stop"

    monkeypatch.setattr(CodingToolOwner, "allow", observed_allow)
    monkeypatch.setattr(native.provider, "response_chunks", chunks)
    async with native.open_owner(runtime_enabled=True, native_options=()) as (agent, session):
        await asyncio.wait_for(agent.prompt(session, [TextContentBlock(
            type="text", text="Write the new ordinary-claimed.txt file once.",
        )]), 35)
        assert target.read_text() == "P620 ordinary native write\n"
        assert len(claims) == 1
        with agent._comms.bus.log.certified_read() as source:
            committed = source.claim(str(target.resolve()))
        assert committed == claims[0]
        assert committed.owner == session
        assert committed.seq > 0
        notices = [message for message in agent._comms.views.full_history_page().messages
                   if message.claim_transition is not None and message.claim_transition.claims]
        assert [(message.sender, message.target, message.body, message.type)
                for message in notices] == [(session, session, "Claimed", ClaimNoticeKind)]
        assert native.provider.posts == 2
        assert native.session.read_bytes().startswith(original)
        print("p620_ordinary_claim=" + json.dumps({
            "seq": committed.seq, "owner": committed.owner,
            "resource": committed.resource, "native_write": target.read_text(),
            "original_history_preserved": True,
        }))
