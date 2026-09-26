"""Owner-only pre-summary/commit sequencing; no autonomous provider trigger.

A trusted caller must already own the ACP turn lock, canonical claimed goal
turn, and exact persistent session. The callback is an owner-selected summary
strategy; tool/model JSON cannot supply it. Nothing invokes this module from a
production turn until a bounded provider strategy and trigger are integrated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .backend import PersistentPiSession
from .compaction_journal import CompactionOperation
from .declarations import Thread
from .owner_compaction_commit import OwnerCompactionCommit
from .owner_compaction_prepare import NativePreparation


@dataclass(frozen=True)
class PreparedOwnerSummary:
    """Only native cutpoint metadata; source content stays with the native Pi."""

    tokens_before: int
    is_split_turn: bool
    session_id: str


async def compact_owner_once(
    bridge: OwnerCompactionCommit,
    owner: Thread,
    epoch: int,
    persistent: PersistentPiSession,
    summarize: Callable[[PreparedOwnerSummary], Awaitable[str]],
    *,
    keep_recent_tokens: int | None = None,
) -> CompactionOperation | None:
    """Exactly one native writer attempt, without input or summary replay.

    Pre-summary capture and final commit each recheck owner/ingress/native
    source. The idle manager is irrevocably discarded BEFORE the native writer
    can mutate the saved file. Its next prompt must pass strict fresh reopen.
    The caller may not hide a COMMIT UNKNOWN or trigger a second summary/write.
    """
    prepared_source = await asyncio.to_thread(
        bridge.prepare_source, owner, epoch, keep_recent_tokens=keep_recent_tokens
    )
    if prepared_source is None:
        return None
    prepared, source = prepared_source
    assert isinstance(prepared, NativePreparation)
    summary = await summarize(
        PreparedOwnerSummary(prepared.tokens_before, prepared.is_split_turn, prepared.session_id)
    )
    if type(summary) is not str or not summary:
        raise ValueError("Bounded owner summary required")
    # No native write can begin until this returns; closing under the borrow
    # lock makes an old RPC manager unusable even if commit is later refused.
    await persistent.discard_for_external_write(prepared.witness["sessionFile"])
    # Do not use asyncio.to_thread in a named inner Task: all-tasks shutdown
    # can cancel that Task and mark it done while its real OS worker still
    # holds the native writer. Retain the concurrent.futures.Future itself,
    # outside asyncio Task cancellation, until the exact operation settles.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="owner-native-commit")
    try:
        committing = executor.submit(
            bridge.commit,
            owner,
            epoch,
            prepared.witness,
            summary,
            prepared.tokens_before,
            source=source,
        )
    finally:
        # No queued follow-up or replay. The submitted worker remains owned
        # by its Future; executor threads retire after this one operation.
        executor.shutdown(wait=False)
    try:
        return await asyncio.shield(asyncio.wrap_future(committing))
    except asyncio.CancelledError:
        # The wrapper may itself become cancelled during loop shutdown, but
        # the concurrent Future cannot report completion while its native
        # worker is still mutating. Keep the caller's turn lock until that
        # exact worker settles, even under repeated owner cancellation.
        while not committing.done():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                continue
        # Consume worker failure without treating cancellation as no-write.
        # The journal's exact intent/outcome is still the recovery authority.
        if not committing.cancelled():
            committing.exception()
        raise
