"""Deliver already-durable compaction metadata to its existing local ACP owner only.

No bus recipient is inferred, no summary is sent, and uncertain local delivery
remains pending under the same exact native commit ID for safe deduplication.
"""

from __future__ import annotations

import json
import os
from typing import Any

from acp.schema import AgentMessageChunk, TextContentBlock

from .compaction_journal import CompactionJournal


async def publish_pending_local(agent: Any, session_id: str, thread_name: str) -> int:
    """Project up to 32 metadata rows; return only the number locally observed."""
    owner = agent._comms.registry.require(thread_name)
    if (
        owner.pid != os.getpid()
        or not owner.session_file
        or agent._require_session(session_id) != thread_name
        or not agent._comms.registry.status(thread_name).running
    ):
        return 0
    runtime = agent._runtime
    if agent._client is None and not runtime.clients.get(session_id):
        return 0  # No consumer: leave every row durably pending.
    path = agent._comms.root / "compaction-commits.sqlite3"
    if not path.exists() and not path.is_symlink():
        return 0
    journal = CompactionJournal(path)
    projected = 0
    for item in journal.pending_publications(owner.session_file):
        # Only the outbox-owned exact metadata is sent; never the intent or
        # operation evidence, both of which may contain sensitive text.
        metadata = json.loads(item.metadata_json)
        await runtime.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta={"agentComms": {"compactionPublication": metadata}},
            ),
        )
        if agent._client is None and not runtime.clients.get(session_id):
            # All socket sends may have failed and been removed by runtime.
            # At least one local transport must still be attached for ACK.
            break
        journal.observe_publication(item.commit_id, item.metadata_json)
        projected += 1
    return projected
