"""Provider-free exact-ID local ACP projection; no summary or invented recipient."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_publication import publish_pending_local
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
