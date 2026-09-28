"""Explicit canonical manual compaction through the selected journaled writer.

This operation sends no user prompt and issues no original-input admission.
Stock Pi's separate saved-session /compact remains outside canonical roots.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from .backend import PersistentPiSession, _session_revision
from .compaction_journal import CompactionJournalError, SelectedSummaryAttempt
from .compaction_states import ManualCommittedSummary
from .errors import RelationViolationError
from .owner_compaction_commit import OwnerCompactionCommit
from .owner_compaction_prepare import NativePreparation
from .owner_compaction_provider import NativeSummary
from .owner_compaction_runtime import compact_owner_once
from .selected_pi_route import read_selected_compaction_decision
from .selected_pi_summary_rpc import SelectedSummarySlot


@dataclass(frozen=True)
class ManualSelectedSummary(NativeSummary):
    attempt: SelectedSummaryAttempt

    def commit_options(self) -> dict:
        return {"selected_attempt": self.attempt}

    def admit_original(self, bridge, owner, owner_generation, operation, source):
        if operation is None or not operation.state.committed:
            raise CompactionJournalError("Manual native commit is not complete")
        bridge.journal.link_selected_summary_commit(
            self.attempt.operation_id, operation.commit_id, state_type=ManualCommittedSummary
        )
        return None


async def compact_manual_owner(
    runner, session_id: str, thread_name: str, info, instructions: str | None
):
    owner, generation = runner.comms.registry.live_owner_with_generation(thread_name)
    if owner.session_file is None or owner.active_turn is None or not owner.model:
        raise ValueError("Manual compaction requires the active owner and saved native session")
    info = await runner.prepare_selected_session(session_id, owner)
    persistent: PersistentPiSession = runner.persistent_backends[session_id]
    if info.context_size is None:
        raise ValueError("Selected native context usage is unavailable")
    provider, model = owner.model.split("/", 1)
    package = runner.effects._private_nk_native_package
    if package is None:
        raise ValueError("Canonical native package is unavailable")
    bridge = await asyncio.to_thread(
        OwnerCompactionCommit, runner.comms.registry.store.path, Path(package)
    )

    pending_input_key = None
    refusals = bridge.journal.blocking_selected_summary(owner.session_file)
    for refusal in refusals:
        refusal.state.manual_recovery()
        prior = json.loads(refusal.source_json)["source"]
        if prior.get("ownerName") != owner.name:
            raise CompactionJournalError("Refused selected source belongs to another owner")
        key = prior.get("ingressKey")
        if key is not None:
            row = bridge.inputs.read().rows.get(key)
            if row is None or not row.unattempted:
                raise CompactionJournalError("Refused original input is no longer unbound")
            if pending_input_key is not None and pending_input_key != key:
                raise CompactionJournalError(
                    "Multiple unresolved originals require explicit review"
                )
            pending_input_key = key

    async def decision():
        return await read_selected_compaction_decision(
            persistent,
            session_file=owner.session_file,
            expected_package=Path(package),
            provider=provider,
            model_id=model,
            context_window=info.context_size,
        )

    settings = await decision()
    summary_text = ""

    async def summarize(prepared: NativePreparation):
        nonlocal summary_text
        current, current_generation = runner.comms.registry.live_owner_with_generation(thread_name)
        if current != owner or current_generation != generation or await decision() != settings:
            raise RelationViolationError("Manual selected source changed before summary")
        for refusal in refusals:
            bridge.journal.retire_refused_summary(refusal)
        source = {
            "source": {
                "ownerName": owner.name,
                "ownerPid": owner.pid,
                "ownerCreatedAt": float(owner.created_at).hex(),
                "turnId": owner.active_turn.id,
                "ingressKey": pending_input_key,
                "reservedRevision": _session_revision(owner.session_file),
            },
            "selected": {
                "provider": provider,
                "modelId": model,
                "contextWindow": info.context_size,
            },
            "settings": {
                "reserveTokens": settings.reserve_tokens,
                "keepRecentTokens": settings.keep_recent_tokens,
            },
        }
        result = await SelectedSummarySlot(
            owner.name, prepared.witness.session_id
        ).run_selected_summary(
            persistent,
            bridge.journal,
            prepared.witness,
            source,
            expected_package=Path(package),
            tokens_before=prepared.tokens_before,
            custom_instructions=instructions.strip() if instructions else None,
        )
        if result.summary is None:
            bridge.journal.refuse_selected_summary(result.operation_id, result.decline_reason)
            raise ValueError(f"Selected Pi declined manual summary ({result.decline_reason})")
        current, current_generation = runner.comms.registry.live_owner_with_generation(thread_name)
        if current != owner or current_generation != generation or await decision() != settings:
            raise RelationViolationError("Manual selected source changed after summary")
        summary_text = result.summary.text
        return ManualSelectedSummary(
            result.summary.text,
            result.summary.details,
            result.summary.usage,
            bridge.journal.selected_summary(result.operation_id),
        )

    operation = await compact_owner_once(
        bridge,
        owner,
        generation,
        persistent,
        summarize,
        keep_recent_tokens=settings.keep_recent_tokens,
        pending_input_key=pending_input_key,
        allow_split_turn=False,
    )
    if operation is None:
        raise ValueError("Selected native history has no complete safe compaction cut")
    return {"ok": True, "summary": summary_text, "commitId": operation.commit_id}
