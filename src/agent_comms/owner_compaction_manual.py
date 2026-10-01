"""Explicit canonical manual compaction through the selected journaled writer.

This operation sends no user prompt and issues no original-input admission.
Stock Pi's separate saved-session /compact remains outside canonical roots.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .backend import PersistentPiSession
from .compaction_errors import CompactionJournalError
from .compaction_records import SelectedSummaryAttempt, SelectedSummarySource
from .compaction_result import CommittedCompactionResult
from .compaction_states import ManualCommittedSummary
from .native_input_owner import RegistryOwner
from .owner_compaction_commit import OwnerCompactionCommit
from .owner_compaction_prepare import NativePreparation
from .owner_compaction_provider import NativeSummary
from .owner_compaction_runtime import compact_owner_once
from .pi_payloads import StateData
from .selected_pi_route import read_selected_compaction_decision
from .selected_pi_summary_rpc import SelectedSummarySlot
from .selected_source import ManualSource, SessionRevision
from .thread_identity import TurnId


@dataclass(frozen=True)
class ManualSelectedSummary(NativeSummary):
    attempt: SelectedSummaryAttempt

    def commit_options(self) -> dict:
        return {"selected_attempt": self.attempt}

    def admit_original(self, bridge, owner, owner_generation, operation, source):
        if operation is None or not operation.state.committed:
            raise CompactionJournalError("Manual native commit is not complete")
        bridge.journal.summaries.link_commit(
            self.attempt.operation_id, operation.commit_id, state_type=ManualCommittedSummary
        )
        return None


async def compact_manual_owner(
    runner, session_id: str, thread_name: str, prepared: StateData, instructions: str | None
) -> CommittedCompactionResult:
    persistent: PersistentPiSession | None = runner.persistent_backends.get(session_id)
    if persistent is None or not persistent.available:
        raise ValueError(
            "Canonical manual compaction requires the prepared selected native session"
        )
    snapshot = runner.comms.registry.snapshot()
    captured = RegistryOwner.capture(snapshot, thread_name, "Manual compaction owner changed")
    owner = captured.thread
    turn = captured.require_active_turn()
    session_file = owner.require_saved_session()
    generation = snapshot.owner_generations[owner.name]
    selected = prepared.model.for_compaction(owner.model)
    package = runner.effects._private_nk_native_package
    if package is None:
        raise ValueError("Canonical native package is unavailable")
    bridge = await asyncio.to_thread(
        OwnerCompactionCommit, runner.comms.registry.store.path, Path(package)
    )

    pending_input_key = None
    refusals = bridge.journal.summaries.blocking(session_file)
    for refusal in refusals:
        refusal.state.manual_recovery()
        prior = refusal.source()
        if prior.incarnation != owner.incarnation:
            raise CompactionJournalError("Refused selected source belongs to another owner")
        key = prior.pending_input_key
        if key is not None:
            row = bridge.inputs.read().lookup(key)
            if not row.accepts_reservation:
                raise CompactionJournalError("Refused original input is no longer unbound")
            if pending_input_key is not None and pending_input_key != key:
                raise CompactionJournalError(
                    "Multiple unresolved originals require explicit review"
                )
            pending_input_key = key

    async def decision():
        return await read_selected_compaction_decision(
            persistent,
            session_file=session_file,
            expected_package=Path(package),
            selected=selected,
        )

    settings = await decision()
    summary_text = ""

    async def summarize(prepared: NativePreparation, captured):
        nonlocal summary_text
        attestation = owner.compaction_attestation(generation, prepared.witness)
        attestation.require_registry(runner.comms.registry, owner)
        settings.require_current(await decision())
        for refusal in refusals:
            bridge.journal.summaries.retire_refused(refusal)
        source = SelectedSummarySource(
            retained=captured.retained,
            source=ManualSource(
                incarnation=owner.incarnation,
                owner=owner.process_identity,
                turn=TurnId(turn.id),
                reserved_revision=SessionRevision.observe(session_file).require_available(),
            ),
            selected=selected,
            settings=settings.summary_settings(),
        )
        result = await SelectedSummarySlot(
            owner.name, prepared.witness.session_id
        ).run_selected_summary(
            persistent,
            bridge.journal,
            prepared.witness,
            source,
            owner=owner,
            expected_package=Path(package),
            tokens_before=prepared.tokens_before,
            custom_instructions=instructions.strip() if instructions else None,
            on_event=lambda event: runner.effects._emit_event(session_id, event),
            reason="manual",
        )
        summary = result.manual_summary(bridge.journal)
        attestation.require_registry(runner.comms.registry, owner)
        settings.require_current(await decision())
        summary_text = summary.text
        return summary

    operation = await compact_owner_once(
        bridge,
        owner,
        generation,
        persistent,
        summarize,
        settings=settings,
        context_window=selected.context_window,
        pending_input_key=pending_input_key,
    )
    if operation is None:
        raise ValueError("Selected native history has no complete safe compaction cut")
    return CommittedCompactionResult(summary_text, operation.commit_id)
