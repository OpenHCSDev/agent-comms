"""Ordinary ACP input commits a native write claim through the real publisher."""

import asyncio
import json

from acp.schema import TextContentBlock

from agent_comms.message_page import MessagePageRequest
from agent_comms.read_basis import AllChannelDisplayScope


async def test_ordinary_native_write_releases_its_exact_claim(native_backend):
    native = native_backend
    await native.author_history()
    original = native.session.read_bytes()
    target = native.project / "ordinary-claimed.txt"
    claims = []

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
            # The actual provider's second request occurs after Pi's write.
            from agent_comms.comms import Comms
            claims.append(Comms(native.root).bus.log.claim_projection()[str(target)])
            yield {"content": "P620 write complete"}, None
            yield {}, "stop"

    native.provider.response_chunks = chunks
    async with native.open_owner(runtime_enabled=True, native_options=()) as (agent, session):
        await asyncio.wait_for(agent.prompt(session, [TextContentBlock(
            type="text", text="Write the new ordinary-claimed.txt file once.",
        )]), 35)
        assert target.read_text() == "P620 ordinary native write\n"
        assert len(claims) == 1
        with agent._comms.bus.log.checked_read() as source:
            committed = source.claim(str(target.resolve()))
        assert committed is None
        acquired = claims[0]
        assert acquired.owner == session
        assert acquired.claim_scope is None, "This must exercise ordinary, not selected, input"
        assert acquired.seq > 0
        assert native.provider.posts == 2
        releases = [release for message in agent._comms.bus.log.read_page(
            MessagePageRequest(AllChannelDisplayScope("#any")),
        ).messages if message.claim_transition is not None
                    for release in message.claim_transition.releases]
        assert any((release.resource, release.generation) == (acquired.resource, acquired.generation)
                   for release in releases)
        assert native.session.read_bytes().startswith(original)
        print("p620_ordinary_claim=" + json.dumps({
            "seq": acquired.seq, "owner": acquired.owner,
            "resource": acquired.resource, "native_write": target.read_text(),
            "original_history_preserved": True,
        }))
