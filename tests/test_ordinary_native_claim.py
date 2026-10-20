"""Ordinary ACP input commits a native write claim through the real publisher."""

import asyncio
import json
import os
import secrets
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from acp.schema import TextContentBlock

from agent_comms.message_page import MessagePageRequest
from agent_comms.envelope_claim_transitions import NativeClaim, ReleasedClaims, UnclaimedResource
from agent_comms.messages import ClaimHistoryKind, ClaimMessage, Message
from agent_comms.check_root import CheckRoot
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_session import NativeBoundary
from agent_comms.envelope_claim_transitions import WritableFileClaim
from delivery_owner_fixture import reply_turn_finished
from native_backend_fixture import NativeBackendFixture
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
        assert isinstance(committed, UnclaimedResource)
        acquired = claims[0]
        assert acquired.owner == session
        assert isinstance(acquired.claim_scope, NativeClaim), "This must exercise ordinary input"
        assert acquired.seq > 0
        assert native.provider.posts == 2
        releases = [release for message in agent._comms.bus.log.read_page(
            MessagePageRequest(AllChannelDisplayScope("#any")),
        ).messages if isinstance(message, ClaimMessage)
                    and isinstance(message.claim_transition, ReleasedClaims)
                    for release in message.claim_transition.releases]
        assert any((release.resource, release.generation) == (acquired.resource, acquired.generation)
                   for release in releases)
        assert native.session.read_bytes().startswith(original)
        print("p620_ordinary_claim=" + json.dumps({
            "seq": acquired.seq, "owner": acquired.owner,
            "resource": acquired.resource, "native_write": target.read_text(),
            "original_history_preserved": True,
        }))


async def test_copied_claim_history_receives_one_fresh_dm(native_backend, monkeypatch):
    native = native_backend
    await native.author_history()
    saved_bytes = native.session.read_bytes()
    source = Comms(native.root)
    author = source.registry.new_thread("copy-author", frozenset(), str(native.project))
    source.registry.declare(replace(
        author, process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    async with native.open_owner() as (_, original_owner):
        plain = source.messaging.send(
            author.name, original_owner, "Old copied history is not a fresh request",
        ).message
        with NativeBoundary(source.bus) as boundary:
            holder = boundary.registry.require(author.name)
            acquisition = source.bus.publisher._publish_acquisition_unlocked(
                boundary.bus_lock, boundary.registry, holder,
                WritableFileClaim(native.project / "claim-only.txt").normalized(native.project),
                NativeClaim(
                    owner_claimed_generation=boundary.registry.claim_generation(holder.name),
                    operation_id=secrets.token_hex(16), turn_id="copy-history-turn",
                    input_id="copy-history-input",
                ), activity=boundary.activity,
            )
    assert native.provider.posts == 0
    assert native.session.read_bytes() == saved_bytes
    original_bus = source.bus.log.path.read_bytes()
    original_registry = source.registry.store.path.read_bytes()
    copy_root = native.project.parent / "checks" / "copied"
    with CheckRoot(source, (author.name, original_owner), copy_root).open() as copied:
        with copied.bus.log.opened_wire_snapshot() as history:
            rows = tuple(history.newest_messages())
        assert plain in rows
        claim_display, = (row for row in rows if row.type is ClaimHistoryKind)
        assert type(claim_display) is Message
        assert Message.from_wire(claim_display.to_wire()) == claim_display
        assert "claim_transition" not in claim_display.to_wire()
        assert not copied.bus.log.claim_projection()
        assert source.bus.log.claim_projection()[acquisition.resource] == acquisition
        owner = copied.registry.require(original_owner)
        clone = NativeBackendFixture(
            copied.root, Path(owner.worktree), Path(owner.session_file),
            native.provider, native.config, native.package,
        )
        native.provider.text = "P715 copied history fresh reply"
        monkeypatch.setenv("AGENT_COMMS_ROOT", str(copied.root))
        async with clone.open_owner(runtime_enabled=True, auto_wake=True) as (agent, session):
            sent = copied.messaging.send(
                author.name, session, "One fresh P715 DM after copying mixed history",
            ).message
            await agent.inputs.drain_inbox(session)
            await asyncio.wait_for(reply_turn_finished(agent, session), 40)
            with copied.bus.log.opened_wire_snapshot() as history:
                replies = tuple(row for row in history.newest_messages()
                                if row.sender == session and row.target == author.name
                                and row.body == native.provider.text)
            assert len(replies) == 1
            assert native.provider.posts == 1
            print("p715_mixed_copy=" + json.dumps({
                "mixed_history_passes": True, "copied_rows": len(rows),
                "dm_message_id": sent.message_id, "reply_id": replies[0].message_id,
                "reply_seen": datetime.now().astimezone().isoformat(),
                "provider_requests": native.provider.posts,
                "original_session_preserved": native.session.read_bytes() == saved_bytes,
                "root": str(copied.root),
            }), flush=True)
    assert source.bus.log.path.read_bytes() == original_bus
    assert source.registry.store.path.read_bytes() == original_registry
    assert native.session.read_bytes() == saved_bytes
