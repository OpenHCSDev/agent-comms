"""Read the selected idle child's actual compaction policy without starting input."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import suppress
from pathlib import Path

from .backend import PersistentPiSession
from .native_pi import NativePiUnavailable
from .native_session_reopen import NativeSessionIdentity
from .native_input_owner import RegistryOwner
from .owner_compaction_prepare import NativePreparationResult
from .owner_compaction_settings import PiCompactionDecision, PiCompactionSettings
from .pi_commands import AgentCommsCompactionSettings, AgentCommsPrepareCompaction, NativeQuery
from .pi_rpc import PiRpcChannel
from .pi_summary_payloads import SelectedModel
from .pi_vocabulary import CompactionReason, ThresholdCompactionReason
from .registration import Registration
from .message_reference import MessageReference


class SelectedPiProbeUnknownError(NativePiUnavailable):
    """A sent or untrusted probe is not retry/commit/input authority."""



async def _exchange_observation(
    persistent: PersistentPiSession,
    request: NativeQuery,
    source: NativeSessionIdentity,
    *,
    expected_package: Path,
    timeout: float,
):
    """One read-only request; every uncertain transport retires the borrowed child."""
    if not 0 < timeout <= request.observation_timeout_seconds:
        raise ValueError("Bounded selected Pi deadline required")
    async with persistent.lock:
        try:
            retained = persistent.custody.idle().selected(
                source, expected_package
            )
        except NativePiUnavailable as error:
            raise SelectedPiProbeUnknownError(str(error)) from error
        proc, reader = retained.child.proc, retained.child.reader
        transmitted = False
        try:
            # The query owns registration, write, read and correlation. From
            # entry into that lifetime onward a failed write/drain is uncertain.
            transmitted = True
            async with asyncio.timeout(timeout):
                response = await request.exchange(
                    reader, proc.stdin, strict=True,
                    max_bytes=PiRpcChannel.OBSERVATION_MAX_BYTES,
                )
            outcome = response.require_request(request).require_request(request)
            if not retained.current:
                raise SelectedPiProbeUnknownError("Selected Pi source changed during dry run")
            return outcome
        except BaseException as error:
            if transmitted:
                # Poison before a cancellable await. No next borrower may use
                # old in-memory history or treat this as a paid-summary receipt.
                persistent.require_reopen(source)
                # close() retains its independently shielded reap task if
                # cancellation interrupts this caller's join.
                with suppress(asyncio.CancelledError):
                    await persistent.close()
            if isinstance(error, (asyncio.CancelledError, SelectedPiProbeUnknownError)):
                raise
            raise SelectedPiProbeUnknownError("Selected Pi dry-run transport uncertain") from error


async def observe_selected_compaction_decision(
    persistent: PersistentPiSession,
    *,
    session_file: str,
    expected_package: Path,
    selected: SelectedModel,
    purpose: type[CompactionReason] = ThresholdCompactionReason,
    boundary: tuple[MessageReference, ...] = (),
    timeout: float = AgentCommsCompactionSettings.default_observation_timeout_seconds,
) -> PiCompactionDecision:
    """Observe actual selected settings/model without auth, provider or input writes."""
    source = persistent.custody.idle().identity
    source.require_session(session_file)
    request = AgentCommsCompactionSettings(
        id=secrets.token_hex(16),
        session_id=source.session_id,
        session_file=source.session_file,
        selected=selected,
        purpose=purpose,
        boundary=boundary,
    )
    return await _exchange_observation(
        persistent, request, source,
        expected_package=expected_package, timeout=timeout,
    )


async def prepare_selected_native_source(
    persistent: PersistentPiSession, *, session_file: str, expected_package: Path,
    selected: SelectedModel, settings: PiCompactionSettings, retained_text: str = "",
    timeout: float = AgentCommsPrepareCompaction.observation_timeout_seconds,
) -> NativePreparationResult:
    """Prepare on the existing idle store; never open a detached history index.

    Both the initial cut and the later exact retained payload use this same
    observation lifetime. Failure retires uncertain custody and grants no
    fallback preparation, provider request or input replay.
    """
    source = persistent.custody.idle().identity
    source.require_session(session_file)
    request = AgentCommsPrepareCompaction(
        id=secrets.token_hex(16), session_id=source.session_id,
        session_file=source.session_file, selected=selected, settings=settings,
        retained_text=retained_text,
    )
    return await _exchange_observation(
        persistent, request, source,
        expected_package=expected_package, timeout=timeout,
    )


async def read_selected_compaction_decision(
    persistent: PersistentPiSession, *, session_file: str, expected_package: Path,
    selected: SelectedModel, registry: Registration, captured: RegistryOwner,
    purpose: type[CompactionReason] = ThresholdCompactionReason,
    timeout: float = AgentCommsCompactionSettings.default_observation_timeout_seconds,
) -> PiCompactionDecision:
    """Use this turn's selected model and one current authored-source cut.

    Registry settings select the next turn. They cannot replace the model
    already observed by this native child or the captured task scope.
    """
    settings = await observe_selected_compaction_decision(
        persistent, session_file=session_file, expected_package=expected_package,
        selected=selected, purpose=purpose, timeout=timeout,
    )
    if settings.trigger or not settings.task_aware:
        return settings
    # Optional timing is opt-in. The ordinary hard/default path above never
    # scans the wire or opens a cadence journal. Use the original authored read.
    from .wire_log import WireLog
    from .compaction_journal import CompactionJournal

    def authored_boundary():
        with WireLog(registry.store.path.with_name("bus.jsonl")).retained_sources(
            captured.thread.name, registry,
        ) as (_, snapshot, facts, _, _):
            captured.require_snapshot(snapshot, "Selected compaction owner changed")
            boundary = facts.optional_boundary(captured.thread, snapshot)
        if boundary and CompactionJournal(
            registry.store.path.with_name("compaction-commits.sqlite3")
        ).summaries.attempted_boundary(session_file, boundary):
            return ()
        return boundary

    boundary = await asyncio.to_thread(authored_boundary)
    if not boundary:
        return settings
    return await observe_selected_compaction_decision(
        persistent, session_file=session_file, expected_package=expected_package,
        selected=selected, purpose=purpose, boundary=boundary, timeout=timeout,
    )
