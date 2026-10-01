"""Owner-only pre-summary/commit sequencing under a validated ACP turn lock.

The native session stays authoritative; a callback cannot grant commit authority.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .agent_events import AgentEvent, CompactionSkipped, CompactionStart
from .backend import PersistentPiSession
from .compaction_records import CompactionOperation, SelectedSummaryAttempt
from .compaction_source import CompactionSource
from .owner_compaction_commit import OwnerCompactionCommit
from .owner_compaction_prepare import NativePreparation
from .owner_compaction_provider import NativeSummary, OwnerSummaryOutcome
from .owner_compaction_settings import PiCompactionSettings
from .selected_summary_admission import SelectedAdmissionIdentity, SelectedSummaryAdmission
from .threads import Thread


@dataclass(frozen=True)
class SelectedNativeSummary(NativeSummary):
    """Selected output owns the reservation-to-commit-to-input binding."""

    attempt: SelectedSummaryAttempt
    identity: SelectedAdmissionIdentity

    def commit_options(self) -> dict:
        return {"selected_attempt": self.attempt}

    def admit_original(
        self,
        bridge: OwnerCompactionCommit,
        owner: Thread,
        owner_generation: int,
        operation: CompactionOperation | None,
        source: CompactionSource,
    ) -> SelectedSummaryAdmission:
        assert operation is not None
        return bridge.admit_selected_original(
            owner, owner_generation, operation, source, self.identity
        )


@dataclass(frozen=True)
class SelectedSummaryDecline(OwnerSummaryOutcome):
    """A verified prestart decline preserves the native manager and source."""

    attempt: SelectedSummaryAttempt
    identity: SelectedAdmissionIdentity
    reason: str

    @property
    def completion_event(self) -> CompactionSkipped:
        return CompactionSkipped(
            reason="adaptive",
            explanation=f"Selected native compaction skipped: {self.reason}. Original context preserved.",
        )

    async def commit_with(
        self, writer: Callable[[NativeSummary], Awaitable[CompactionOperation]]
    ) -> None:
        return None

    def admit_original(
        self,
        bridge: OwnerCompactionCommit,
        owner: Thread,
        owner_generation: int,
        operation: CompactionOperation | None,
        source: CompactionSource,
    ) -> SelectedSummaryAdmission:
        return bridge.admit_selected_decline(
            owner, owner_generation, self.attempt, source, self.identity, self.reason
        )


async def compact_owner_once(
    bridge: OwnerCompactionCommit,
    owner: Thread,
    owner_generation: int,
    persistent: PersistentPiSession,
    summarize: Callable[[NativePreparation, CompactionSource], Awaitable[OwnerSummaryOutcome]],
    *,
    settings: PiCompactionSettings,
    context_window: int,
    pending_input_key: str | None = None,
    settings_paths: tuple[str, ...] | None = None,
    on_admission: Callable[[SelectedSummaryAdmission], None] | None = None,
    on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
) -> CompactionOperation | None:
    """Exactly one native writer attempt, without input or summary replay.

    Pre-summary capture and final commit each recheck owner/ingress/native
    source. The idle manager is irrevocably discarded BEFORE the native writer
    can mutate the saved file. Its next prompt must pass strict fresh reopen.
    The caller may not hide a COMMIT UNKNOWN or trigger a second summary/write.
    """
    prepared_source = await asyncio.to_thread(
        bridge.prepare_source,
        owner,
        owner_generation,
        settings=settings,
        context_window=context_window,
        pending_input_key=pending_input_key,
        settings_paths=settings_paths,
    )
    if prepared_source is None:
        return None
    prepared, source = prepared_source
    if on_event is not None:
        await on_event(CompactionStart(reason="adaptive"))
    await asyncio.to_thread(bridge.require_source_current, owner, owner_generation, source)
    result = await summarize(prepared, source)

    async def write(summary: NativeSummary) -> CompactionOperation:
        return await _commit_native_summary(
            bridge, owner, owner_generation, persistent, prepared, source, summary
        )

    operation = await result.commit_with(write)
    admission = result.admit_original(bridge, owner, owner_generation, operation, source)
    if admission is not None:
        if on_admission is None:
            admission.invalidate()
            raise ValueError("Selected summary requires its original-input owner")
        on_admission(admission)
    if on_event is not None:
        await on_event(result.completion_event)
    return operation


async def _commit_native_summary(
    bridge: OwnerCompactionCommit,
    owner: Thread,
    owner_generation: int,
    persistent: PersistentPiSession,
    prepared: NativePreparation,
    source: CompactionSource,
    result: NativeSummary,
) -> CompactionOperation:
    if not result.text:
        raise ValueError("Bounded owner summary required")
    # No native write can begin until this returns; closing under the borrow
    # lock makes an old RPC manager unusable even if commit is later refused.
    await persistent.discard_for_external_write(prepared.witness.session_file)
    # Do not use asyncio.to_thread in a named inner Task: all-tasks shutdown
    # can cancel that Task and mark it done while its real OS worker still
    # holds the native writer. Retain the concurrent.futures.Future itself,
    # outside asyncio Task cancellation, until the exact operation settles.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="owner-native-commit")
    try:
        committing = executor.submit(
            bridge.commit,
            owner,
            owner_generation,
            prepared.witness,
            result.text,
            prepared.tokens_before,
            source=source,
            details=result.details,
            usage=result.usage,
            **result.commit_options(),
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
