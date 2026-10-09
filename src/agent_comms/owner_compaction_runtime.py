"""Owner-only pre-summary/commit sequencing under a validated ACP turn lock.

The native session stays authoritative; a callback cannot grant commit authority.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial

from .agent_events import AgentEvent, CompactionSkipped, CompactionStart
from .backend import PersistentPiSession
from .compaction_records import CompactionOperation, SelectedSummaryAttempt
from .compaction_source import CompactionSource
from .compaction_result import CompactionResult, RefusedCompactionResult
from .coordinator import Coordination
from .owner_compaction_commit import OwnerCompactionCommit
from .owner_compaction_prepare import NativePreparation
from .owner_compaction_provider import NativeSummary, OwnerSummaryOutcome
from .owner_compaction_settings import PiCompactionDecision
from .selected_summary_admission import SelectedAdmissionIdentity, SelectedSummaryAdmission
from .selected_pi_route import prepare_selected_native_source
from .pi_summary_payloads import SelectedModel
from .pi_vocabulary import CompactionReason
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
    def explanation(self) -> str:
        return f"Selected native compaction skipped: {self.reason}. Original context preserved."

    def completion_event(self, reason: type[CompactionReason]) -> CompactionSkipped:
        return CompactionSkipped(
            reason=reason,
            explanation=self.explanation,
        )

    async def commit_with(
        self, writer: Callable[[NativeSummary], Awaitable[CompactionOperation]]
    ) -> None:
        return None

    def compaction_result(self, operation: CompactionOperation | None) -> RefusedCompactionResult:
        return RefusedCompactionResult(self.explanation)

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
    settings: PiCompactionDecision,
    selected: SelectedModel,
    pending_input_keys: tuple[str, ...] = (),
    settings_paths: tuple[str, ...] | None = None,
    on_admission: Callable[[SelectedSummaryAdmission], None] | None = None,
    on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
) -> CompactionResult:
    """Exactly one native writer attempt, without input or summary replay.

    Pre-summary capture and final commit each recheck owner/ingress/native
    source. Original custody makes the old manager unavailable during the write
    and reloads the same child from its exact committed source before reuse.
    The caller may not hide a COMMIT UNKNOWN or trigger a second summary/write.
    """
    # The retained idle child owns both preparations. Each supplies its actual
    # settings/model; captured original facts enter only the second budget.
    async def prepare(retained_text: str):
        return await prepare_selected_native_source(
            persistent, session_file=owner.require_saved_session(),
            expected_package=bridge.native.package_dir,
            selected=selected, settings=settings.summary_settings(),
            retained_text=retained_text,
        )

    preparation = await prepare("")

    async def perform(prepared: NativePreparation) -> CompactionResult:
        prepared, source = await bridge.prepare_source(
            owner,
            owner_generation,
            prepared=prepared,
            prepare=prepare,
            pending_input_keys=pending_input_keys,
            settings_paths=settings_paths,
        )
        async def at_cut(prepared: NativePreparation) -> CompactionResult:
            if not await settings.boundary_current(source.retained, owner, bridge.registry):
                return RefusedCompactionResult("Authored subtask boundary changed; optional compaction skipped")
            if on_event is not None:
                await on_event(CompactionStart(reason=settings.reason))
            await Coordination.run_worker(partial(
                bridge.require_source_current, owner, owner_generation, source
            ))
            result = await summarize(prepared, source)

            async def write(summary: NativeSummary) -> CompactionOperation:
                return await _commit_native_summary(
                    bridge, owner, owner_generation, persistent, prepared, source, summary,
                    reason=settings.reason,
                )

            operation = await result.commit_with(write)
            admission = await Coordination.run_worker(partial(
                result.admit_original, bridge, owner, owner_generation, operation, source
            ))
            if admission is not None:
                if on_admission is None:
                    admission.invalidate()
                    raise ValueError("Selected summary requires its original-input owner")
                on_admission(admission)
            if on_event is not None:
                await on_event(result.completion_event(settings.reason))
            return result.compaction_result(operation)
        return await settings.prepare(prepared).compact_owner(at_cut)

    return await settings.prepare(preparation).compact_owner(perform)


async def _commit_native_summary(
    bridge: OwnerCompactionCommit,
    owner: Thread,
    owner_generation: int,
    persistent: PersistentPiSession,
    prepared: NativePreparation,
    source: CompactionSource,
    result: NativeSummary,
    *,
    reason: type[CompactionReason],
) -> CompactionOperation:
    if not result.text:
        raise ValueError("Bounded owner summary required")
    async with persistent.external_write(prepared.witness) as retained:
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
            operation = await asyncio.shield(asyncio.wrap_future(committing))
        except asyncio.CancelledError as cancellation:
            # The wrapper may itself become cancelled during loop shutdown, but
            # the concurrent Future cannot report completion while its native
            # worker is still mutating. Keep the caller's turn lock until that
            # exact worker settles, even under repeated owner cancellation.
            while not committing.done():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue
            # Carry the worker's failure on the cancellation without treating
            # cancellation as no-write. The journal's exact intent/outcome is
            # still the recovery authority.
            if not committing.cancelled() and (outcome := committing.exception()) is not None:
                cancellation.__cause__ = outcome
            raise
        await retained.reload(
            source.after_native_commit(operation.committed_outcome()).native, operation, reason
        )
        return operation
