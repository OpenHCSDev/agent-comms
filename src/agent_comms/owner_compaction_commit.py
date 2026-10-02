"""Owner sequencing over held authority, typed native commands and journal roles.

Native transport/outcome settlement, source equality, registry custody and input
admission each have one owner. This coordinator never interprets provider output
or treats a visible terminal SQL row as authority to replay an original input.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from .agent_events import AgentEvent
from .backend import PersistentPiSession
from .compaction_result import CompactionResult
from .selected_source import SelectedSource, SessionRevision
from .compaction_boundary import CompactionBoundary
from .compaction_errors import CompactionJournalError
from .compaction_identity import SelectedCommitReference
from .compaction_journal import CompactionJournal
from .compaction_records import CompactionOperation, SelectedSummaryAttempt
from .compaction_source import CompactionSource
from .input_disposition import FutureInputQueue, InputDispositions
from .native_compaction_request import NativeIntent, NativeSummaryPayload
from .native_compaction_writer import NativeCompactionWriter
from .owner_compaction_prepare import NativePreparation, NativeWitness, prepare_native_source
from .owner_compaction_settings import PiCompactionDecision, PiCompactionSettings
from .pi_summary_payloads import SelectedModel, SummaryFiles, SummaryUsage
from .registration import Registration
from .reservation_rules import CommitReservationCheck
from .selected_summary_admission import SelectedAdmissionIdentity, SelectedSummaryAdmission
from .text_digest import TextDigest
from .thread_identity import TurnId
from .threads import Thread


class OwnerCompactionCommit:
    def __init__(self, registry_path: Path, package_dir: Path, *, future_queue: FutureInputQueue | None = None):
        root = registry_path.parent.resolve(strict=True)
        self.native = NativeCompactionWriter(package_dir)
        self.registry = Registration(registry_path)
        self.inputs = InputDispositions(root / InputDispositions.filename)
        self.boundary = CompactionBoundary(self.registry, self.inputs, future_queue)
        self.journal = CompactionJournal(registry_path.with_name("compaction-commits.sqlite3"))

    async def compact_selected(
        self, owner: Thread, owner_generation: int, persistent: PersistentPiSession,
        source: SelectedSource,
        selected: SelectedModel, settings: PiCompactionDecision, *,
        instructions: str | None = None,
        pending_input_keys: tuple[str, ...] = (),
        before_summary: Callable[[], None] | None = None,
        on_admission: Callable[[SelectedSummaryAdmission], None] | None = None,
        on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
        reason: str = "adaptive",
    ) -> CompactionResult:
        """One selected provider/source/commit operation for every owner caller.

        The source family owns whether a committed summary admits an existing
        original or merely makes the saved owner ready for a future reservation.
        """
        from .compaction_records import SelectedSummarySource
        from .owner_compaction_runtime import compact_owner_once
        from .selected_pi_route import read_selected_compaction_decision
        from .selected_pi_summary_rpc import SelectedSummarySlot

        package = self.native.package_dir
        session_file = owner.require_saved_session()
        launch = persistent.custody.idle().child.key[0]
        settings_paths = launch.configuration.settings_paths(launch.cwd)

        async def decision():
            return await read_selected_compaction_decision(
                persistent, session_file=session_file,
                expected_package=package, selected=selected,
            )

        async def summarize(prepared: NativePreparation, captured: CompactionSource):
            attestation = owner.compaction_attestation(owner_generation, prepared.witness)
            attestation.require_registry(self.registry, owner)
            settings.require_current(await decision())
            if before_summary is not None:
                before_summary()
            result = await SelectedSummarySlot(
                owner.name, prepared.witness.session_id
            ).run_selected_summary(
                persistent, self.journal, prepared.witness,
                SelectedSummarySource(
                    source=source, selected=selected,
                    settings=settings.summary_settings(), retained=captured.retained,
                ),
                owner=owner, expected_package=package,
                tokens_before=prepared.tokens_before,
                custom_instructions=instructions,
                future_queue=self.boundary.future_queue,
                on_event=on_event, reason=reason,
            )
            outcome = source.summary_outcome(result, self.journal)
            attestation.require_registry(self.registry, owner)
            settings.require_current(await decision())
            return outcome

        return await compact_owner_once(
            self, owner, owner_generation, persistent, summarize,
            settings=settings, context_window=selected.context_window,
            pending_input_keys=pending_input_keys, settings_paths=settings_paths,
            on_admission=on_admission, on_event=on_event,
        )

    def capture_source(
        self,
        owner: Thread,
        owner_generation: int,
        witness: NativeWitness,
        *,
        pending_input_keys: tuple[str, ...] = (),
        settings_paths: tuple[str, ...] | None = None,
    ) -> CompactionSource:
        """Capture BEFORE generating a summary; no provider work under these locks.

        Owner-relevant ingress remains fenced. Exact live future queue receipts
        may wait through the summary; no UNKNOWN input is replayed or resolved.
        """
        with self.boundary.hold(
            owner, owner_generation, witness, pending_input_keys=pending_input_keys
        ) as held:
            if self.journal.operations.unresolved(witness.session_file):
                raise CompactionJournalError("Unresolved native commit; reconcile before preparation")
            return held.capture(pending_input_keys, settings_paths)

    def prepare_source(
        self,
        owner: Thread,
        owner_generation: int,
        *,
        prepared: NativePreparation,
        settings: PiCompactionSettings,
        context_window: int,
        pending_input_keys: tuple[str, ...] = (),
        settings_paths: tuple[str, ...] | None = None,
    ) -> tuple[NativePreparation, CompactionSource]:
        """Read Pi's saved cut point, then capture owner/ingress source before summarizing.

        Preparation uses exact selected settings/window, never invokes a
        provider or mutates a session, and does not grant a
        commit: the writer must still CAS against the saved native witness.
        """
        prepared.witness.require_session(owner.require_saved_session())
        self.reconcile_interrupted_summaries(owner, owner_generation, prepared.witness)
        source = self.capture_source(
            owner,
            owner_generation,
            prepared.witness,
            pending_input_keys=pending_input_keys,
            settings_paths=settings_paths,
        )
        # Capture determines the required original facts. Native policy then
        # allocates that exact payload together with its atomic recent suffix;
        # both reads remain tied to the original source revision and leaf.
        allocated = prepare_native_source(
            self.native.package_dir, source.native.session_file,
            settings=settings, context_window=context_window, retained_text=source.retained.text,
        ).require_ready()
        source = source.at_prepared_cut(allocated.witness)
        self.require_source_current(owner, owner_generation, source)
        return allocated, source

    def require_source_current(self, owner: Thread, owner_generation: int,
                               source: CompactionSource) -> None:
        with self.boundary.hold(
            owner, owner_generation, source.native,
            pending_input_keys=source.pending_input_keys,
        ) as held:
            source.require_current(held)


    def reconcile_interrupted_summaries(
        self, owner: Thread, owner_generation: int, witness: NativeWitness
    ) -> None:
        """Retire an interrupted summary only when no original input or write occurred.

        The provider outcome remains UNKNOWN. A later explicit input may request
        a new summary; neither this method nor the retired row replays anything.
        """
        with self.boundary.hold(owner, owner_generation, witness, settled=False) as held:
            for attempt in self.journal.summaries.history(witness.session_file):
                if not attempt.state.reconcile_unchanged_source:
                    continue
                attempt.request.interrupted_check(
                    SessionRevision.observe(witness.session_file),
                    self.inputs._read_unlocked(),
                    owner.incarnation,
                    TurnId(held.receipt.turn_id),
                ).require_valid()
                self.journal.summaries.retire_unchanged(attempt)


    def commit(
        self,
        owner: Thread,
        owner_generation: int,
        witness: NativeWitness,
        summary: str,
        tokens_before: int,
        *,
        source: CompactionSource,
        details: SummaryFiles | None = None,
        usage: SummaryUsage | None = None,
        selected_attempt: SelectedSummaryAttempt | None = None,
        timeout: float = 5,
    ) -> CompactionOperation:
        """Journal intent under authority, dispatch once, persist observed outcome."""
        payload = NativeSummaryPayload(summary=summary, tokens_before=tokens_before, details=details, usage=usage)
        native_intent = NativeIntent(witness, payload.payload_digest(witness), payload.metadata_digest())
        with self.boundary.hold(
            owner, owner_generation, witness, pending_input_keys=source.pending_input_keys
        ) as held:
            source.require_current(held)
            source.retained.require_summary(summary)
            selected = None
            if selected_attempt is not None:
                selected_attempt.state.require_commit_reservation()
                self.journal.summaries.require_current(selected_attempt, witness.session_file)
                CommitReservationCheck(
                    source=selected_attempt.request.source,
                    revision=SessionRevision.observe(witness.session_file),
                    incarnation=owner.incarnation,
                    owner=owner.process_identity,
                    turn=TurnId(source.turn_id),
                    pending_input_keys=source.pending_input_keys,
                ).require_valid()
                selected = SelectedCommitReference(
                    selected_attempt.operation_id, TextDigest.of(selected_attempt.source_json).value,
                )
            commit_id = self.journal.operations.begin(
                witness.session_file, native_intent, owner=held.receipt, source=source,
                selected=selected, inputs=self.inputs._read_unlocked(),
            )
            return self.native.settle(
                held, payload.request(witness, native_intent.identity(commit_id)), self.journal, timeout,
            )

    def admit_selected_decline(
        self,
        owner: Thread,
        owner_generation: int,
        attempt: SelectedSummaryAttempt,
        source: CompactionSource,
        identity: SelectedAdmissionIdentity,
        reason: str,
    ) -> SelectedSummaryAdmission:
        """Continue one original after a correlated, unchanged prestart decline."""
        with self.boundary.hold(
            owner, owner_generation, source.native, pending_input_keys=source.pending_input_keys
        ) as held:
            source.require_current(held)
            self.journal.summaries.require_current(attempt, source.native.session_file)
            identity.require_reserved_revision(source.native.session_file)
            admission = self.journal.summaries.decline_prestart(attempt.operation_id, reason, admission=identity)
            assert admission is not None
            return admission

    def admit_selected_original(
        self,
        owner: Thread,
        owner_generation: int,
        operation: CompactionOperation,
        source: CompactionSource,
        identity: SelectedAdmissionIdentity,
    ) -> SelectedSummaryAdmission:
        """Link one committed result after rechecking the same owner and ingress."""
        evidence = operation.committed_outcome()
        intent = NativeIntent.read(operation)
        if intent.witness != source.native:
            raise CompactionJournalError("Selected native commit belongs to another source")
        committed_source = source.after_native_commit(evidence)
        with self.boundary.hold(
            owner, owner_generation, committed_source.native,
            pending_input_keys=source.pending_input_keys,
        ) as held:
            committed_source.require_current(held)
            if self.journal.operations.get(operation.commit_id) != operation:
                raise CompactionJournalError("Selected native commit changed")
            current_identity = identity.after_native_commit(intent.witness.session_file, evidence)
            admission = self.journal.summaries.link_commit(
                SelectedCommitReference.from_intent(json.loads(operation.intent_json)).operation_id,
                operation.commit_id, admission=current_identity,
            )
            assert admission is not None
            return admission

    def reconcile(
        self, owner: Thread, owner_generation: int, commit_id: str, *, timeout: float = 5
    ) -> CompactionOperation:
        """Explicit exact-ID observation. NEVER sends summary or a commit action."""
        operation = self.journal.operations.get(commit_id)
        if operation.state.terminal:
            return operation
        intent = NativeIntent.read(operation)
        with self.boundary.hold(owner, owner_generation, intent.witness, settled=False) as held:
            current = self.journal.operations.get(commit_id)
            if current.state.terminal:
                return current
            if current.intent_json != operation.intent_json:
                raise CompactionJournalError("Compaction intent changed")
            return self.native.settle(held, intent.reconciliation(commit_id), self.journal, timeout)
