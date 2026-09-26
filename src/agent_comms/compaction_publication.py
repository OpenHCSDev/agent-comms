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
from .compaction_publication_lease import publication_identity_fence
from .declarations import RelationViolationError, UnregisteredThreadError


async def publish_pending_local(agent: Any, session_id: str, thread_name: str) -> int:
    """Project up to 32 metadata rows; return only the number locally observed."""
    runtime = agent._runtime
    if agent._client is None and not runtime.clients.get(session_id):
        return 0  # No consumer: leave every row durably pending.
    path = agent._comms.root / "compaction-commits.sqlite3"
    if not path.exists() and not path.is_symlink():
        return 0
    projected = 0
    # Separate short-lived OS fence, NOT the registry/wire lock. Identity
    # mutations fail promptly while a local client handoff is in flight.
    # Cancellation releases the kernel fence; the row stays pending unless
    # its exact mark already COMMITted (reconcile rather than infer rollback).
    with publication_identity_fence(agent._comms.root, nonblocking=True):
        try:
            owner, epoch = agent._comms.registry.live_owner_with_epoch(thread_name)
        except (RelationViolationError, UnregisteredThreadError):
            return 0
        if (
            owner.pid != os.getpid()
            or not owner.session_file
            or agent._require_session(session_id) != owner.name
        ):
            return 0
        identity = (
            owner.name,
            owner.created_at,
            owner.pid,
            epoch,
            owner.session_file,
            owner.worktree,
        )
        journal = CompactionJournal(path)
        client = agent._client
        for item in journal.pending_publications(owner.session_file):
            # Recheck under the handoff fence; an owner epoch may change even
            # without a session rebind. No old row crosses that boundary.
            try:
                current, current_epoch = agent._comms.registry.live_owner_with_epoch(thread_name)
            except (RelationViolationError, UnregisteredThreadError):
                break
            if (
                (
                    current.name,
                    current.created_at,
                    current.pid,
                    current_epoch,
                    current.session_file,
                    current.worktree,
                )
                != identity
                or agent._require_session(session_id) != owner.name
                or agent._client is not client
            ):
                break
            # Only exact outbox metadata, never intent/summary/recipient.
            metadata = json.loads(item.metadata_json)
            await runtime.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=""),
                    field_meta={"agentComms": {"compactionPublication": metadata}},
                ),
            )
            try:
                current, current_epoch = agent._comms.registry.live_owner_with_epoch(thread_name)
            except (RelationViolationError, UnregisteredThreadError):
                break
            if (
                (
                    current.name,
                    current.created_at,
                    current.pid,
                    current_epoch,
                    current.session_file,
                    current.worktree,
                )
                != identity
                or agent._require_session(session_id) != owner.name
                or agent._client is not client
                or (client is None and not runtime.clients.get(session_id))
            ):
                # No ACK after owner/client change or all socket sends failed.
                break
            journal.observe_publication(item.commit_id, item.metadata_json)
            projected += 1
    return projected
