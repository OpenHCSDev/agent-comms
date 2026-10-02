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
from .compaction_records import SelectedSummaryAttempt
from .compaction_result import CompactionResult
from .compaction_states import ManualCommittedSummary
from .native_input_owner import RegistryOwner
from .owner_compaction_commit import OwnerCompactionCommit
from .owner_compaction_provider import NativeSummary
from .pi_payloads import StateData
from .selected_pi_route import read_selected_compaction_decision
from .pi_vocabulary import ManualCompactionReason
from .selected_source import ManualSource, SessionRevision
from .thread_identity import TurnId


@dataclass(frozen=True)
class ManualSelectedSummary(NativeSummary):
    """A journaled owner summary without an original prompt to admit."""

    attempt: SelectedSummaryAttempt

    def commit_options(self) -> dict:
        return {"selected_attempt": self.attempt}

    def admit_original(self, bridge, owner, owner_generation, operation, source):
        assert operation is not None
        operation.state.require_committed(operation.commit_id)
        bridge.journal.summaries.link_commit(
            self.attempt.operation_id, operation.commit_id, state_type=ManualCommittedSummary
        )
        return None


async def compact_manual_owner(
    runner, session_id: str, thread_name: str, prepared: StateData, instructions: str | None
) -> CompactionResult:
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

    pending_input_keys = ()
    refusals = bridge.journal.summaries.blocking(session_file)
    for refusal in refusals:
        refusal.state.manual_recovery()
        prior = refusal.source()
        if prior.incarnation != owner.incarnation:
            raise CompactionJournalError("Refused selected source belongs to another owner")
        keys = prior.pending_input_keys
        if keys:
            inputs = bridge.inputs.read()
            if any(not inputs.lookup(key).accepts_reservation for key in keys):
                raise CompactionJournalError("Refused original input is no longer unbound")
            if pending_input_keys and pending_input_keys != keys:
                raise CompactionJournalError(
                    "Multiple unresolved originals require explicit review"
                )
            pending_input_keys = keys

    settings = await read_selected_compaction_decision(
        persistent, session_file=session_file,
        expected_package=Path(package), selected=selected,
        registry=bridge.registry, thread_name=owner.name, purpose=ManualCompactionReason,
    )
    source = ManualSource(
        incarnation=owner.incarnation,
        owner=owner.process_identity,
        turn=TurnId(turn.id),
        reserved_revision=SessionRevision.observe(session_file).require_available(),
    )

    def retire_refusals():
        for refusal in refusals:
            bridge.journal.summaries.retire_refused(refusal)

    return await bridge.compact_selected(
        owner, generation, persistent, source, selected, settings,
        instructions=instructions.strip() if instructions else None,
        pending_input_keys=pending_input_keys, before_summary=retire_refusals,
        on_event=lambda event: runner.effects._emit_event(session_id, event),
        reason="manual",
        purpose=ManualCompactionReason,
    )
