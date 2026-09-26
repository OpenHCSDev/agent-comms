"""Provider-free exact-ID local ACP projection; no summary or invented recipient."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_publication import publish_pending_local
from agent_comms.compaction_publication_lease import publication_identity_fence
from agent_comms.declarations import RelationViolationError
from agent_comms.operations import wire

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX durable journal")


@pytest.fixture
def owner(tmp_path):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    session = tmp_path / "saved.jsonl"
    session.write_text("{}\n")
    journal = CompactionJournal(comms.root / "compaction-commits.sqlite3")
    commit_id = journal.begin(str(session), {"summary": "private summary must not publish"})
    journal.resolve(
        commit_id,
        "committed",
        {"status": "committed", "entryId": "entry", "revision": "rev", "leafId": "leaf"},
        publication=True,
    )
    return agent, comms, session, journal, commit_id


@pytest.mark.asyncio
async def test_local_delivery_requires_existing_owner_and_attached_transport(owner, tmp_path):
    agent, comms, session, journal, commit_id = owner
    await agent.new_session(str(tmp_path / "project"))
    comms.attach_session("project", str(session), pid=os.getpid())
    try:
        assert await publish_pending_local(agent, "project", "project") == 0
        assert [row.commit_id for row in journal.pending_publications(str(session))] == [commit_id]
        updates = []

        class Client:
            async def session_update(self, session_id, update):
                updates.append(update.model_dump(by_alias=True, exclude_none=True))

        agent.on_connect(Client())
        assert await publish_pending_local(agent, "project", "project") == 1
        assert journal.pending_publications(str(session)) == ()
        wire_value = json.dumps(updates)
        assert commit_id in wire_value and "entry" in wire_value
        assert "private summary" not in wire_value and "recipient" not in wire_value
        assert "compactionPublication" in wire_value
        assert await publish_pending_local(agent, "project", "project") == 0
        bus = comms.root / "bus.jsonl"
        assert not bus.exists() or b"private summary" not in bus.read_bytes()
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_pending_metadata_projects_before_next_owner_input_send(owner, tmp_path, monkeypatch):
    agent, comms, session, journal, commit_id = owner
    await agent.new_session(str(tmp_path / "project"))
    agent._drain_tasks["project"].cancel()
    await asyncio.gather(agent._drain_tasks["project"], return_exceptions=True)
    comms.attach_session("project", str(session), pid=os.getpid())
    updates = []

    class Client:
        async def session_update(self, session_id, update):
            updates.append(update.model_dump(by_alias=True, exclude_none=True))

    async def events(*args, **kwargs):
        assert any(
            item.get("_meta", {})
            .get("agentComms", {})
            .get("compactionPublication", {})
            .get("commitId")
            == commit_id
            for item in updates
        ), "Local metadata projection must precede native provider send"
        with kwargs["send_boundary"](None, "a" * 32, args[2]) as allowed:
            assert allowed is True
        yield {"type": "done", "ok": False, "text": "No provider invoked"}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    agent.on_connect(Client())
    try:
        await agent._run_owned_input("project", "project", "distinct new input")
        assert journal.pending_publications(str(session)) == ()
        assert (
            len(
                [
                    item
                    for item in updates
                    if item.get("_meta", {}).get("agentComms", {}).get("compactionPublication")
                ]
            )
            == 1
        )
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_session_rebinding_during_actual_handoff_refuses_before_delivery(owner, tmp_path):
    agent, comms, first, journal, commit_id = owner
    second = tmp_path / "second.jsonl"
    second.write_text("{}\n")
    await agent.new_session(str(tmp_path / "project"))
    comms.attach_session("project", str(first), pid=os.getpid())
    delivered = []

    class Client:
        async def session_update(self, session_id, update):
            delivered.append((comms.registry.require("project").session_file, update))

    agent.on_connect(Client())
    actual = agent._runtime.session_update

    async def race(*, session_id, update):
        await asyncio.sleep(0)
        comms.attach_session("project", str(second), pid=os.getpid())
        await actual(session_id=session_id, update=update)

    agent._runtime.session_update = race
    try:
        with pytest.raises(RelationViolationError, match="identity is publishing"):
            await publish_pending_local(agent, "project", "project")
        assert delivered == []
        assert comms.registry.require("project").session_file == str(first)
        assert [item.commit_id for item in journal.pending_publications(str(first))] == [commit_id]
        agent._runtime.session_update = actual
        assert await publish_pending_local(agent, "project", "project") == 1
        assert delivered[0][0] == str(first)
        # Rebinding after a fully completed handoff is not permanently blocked.
        assert comms.attach_session("project", str(second), pid=os.getpid()).session_file == str(
            second
        )
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_after_delivery_changed_acp_binding_never_marks_old_commit(owner, tmp_path):
    agent, comms, session, journal, commit_id = owner
    await agent.new_session(str(tmp_path / "project"))
    comms.attach_session("project", str(session), pid=os.getpid())
    received = []

    class Client:
        async def session_update(self, session_id, update):
            received.append(update.model_dump(by_alias=True, exclude_none=True))
            # Independent ACP binding shift AFTER this local handoff, BEFORE
            # its await returns to the outbox observer. No changed owner is
            # allowed to ACK the old session's exact commit row.
            agent._sessions[session_id] = "other-canonical-thread"

    agent.on_connect(Client())
    try:
        assert await publish_pending_local(agent, "project", "project") == 0
        assert len(received) == 1
        assert [row.commit_id for row in journal.pending_publications(str(session))] == [commit_id]
        # Rebinding the ACP map back reprojects only the same exact ID; remote
        # consumers must deduplicate, and no summary text is ever projected.
        agent._sessions["project"] = "project"
        assert await publish_pending_local(agent, "project", "project") == 0
        assert len(received) == 2
        assert received[0] == received[1]
        assert [row.commit_id for row in journal.pending_publications(str(session))] == [commit_id]
    finally:
        agent._sessions["project"] = "project"
        await agent.shutdown()


def test_cross_process_identity_rebind_is_denied_during_projection_fence(owner, tmp_path):
    _agent, comms, first, _journal, _commit_id = owner
    # Fixture owner has not registered a thread until ACP creates its session.
    from agent_comms.declarations import Thread, ThreadRegistry

    registry = ThreadRegistry(comms.registry._path)
    registry.register(
        Thread("project", frozenset(), str(tmp_path), pid=os.getpid(), session_file=str(first))
    )
    other = tmp_path / "other.jsonl"
    other.write_text("{}\n")
    program = """
import sys
from dataclasses import replace
from pathlib import Path
from agent_comms.declarations import ThreadRegistry, RelationViolationError
registry=ThreadRegistry(Path(sys.argv[1]))
owner=registry.require('project')
try:
    registry.register(replace(owner,session_file=sys.argv[2]))
except RelationViolationError as error:
    assert 'identity is publishing' in str(error)
    print('denied')
else:
    raise AssertionError('Identity changed during publication')
"""
    with publication_identity_fence(comms.root):
        result = subprocess.run(
            [sys.executable, "-c", program, str(comms.registry._path), str(other)],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        assert result.stdout.strip() == "denied"
        assert registry.require("project").session_file == str(first)
    assert registry.require("project").session_file == str(first)


@pytest.mark.asyncio
async def test_uncertain_local_delivery_remains_pending_until_exact_reprojection(owner, tmp_path):
    agent, comms, session, journal, commit_id = owner
    await agent.new_session(str(tmp_path / "project"))
    comms.attach_session("project", str(session), pid=os.getpid())
    seen = []

    class Client:
        fail = True

        async def session_update(self, session_id, update):
            seen.append(update.model_dump(by_alias=True, exclude_none=True))
            if self.fail:
                raise ConnectionError("delivery uncertain")

    client = Client()
    agent.on_connect(client)
    try:
        with pytest.raises(ConnectionError, match="uncertain"):
            await publish_pending_local(agent, "project", "project")
        assert [
            row.commit_id
            for row in CompactionJournal(journal.path).pending_publications(str(session))
        ] == [commit_id]
        client.fail = False
        assert await publish_pending_local(agent, "project", "project") == 1
        assert len(seen) == 2
        first = seen[0]["_meta"]["agentComms"]["compactionPublication"]
        second = seen[1]["_meta"]["agentComms"]["compactionPublication"]
        assert first == second and first["commitId"] == commit_id
    finally:
        await agent.shutdown()
