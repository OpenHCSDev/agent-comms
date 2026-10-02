"""Enroll an already running native test host in the actual retained custody owner."""

import asyncio
from collections import Counter
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Annotated

from agent_comms.compaction_identity import SummaryOperationIdentity
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import CompactionOperation, SelectedSummaryAttempt
from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.native_entries import ManagedCompactionEntry, MessageEntry, NativeEntry, NativeEvidenceRead
from agent_comms.native_input_record import NativeInputIdText
from agent_comms.native_pi import NativeContextProof, NativeContextRecord

from agent_comms.backend import PersistentPiSession
from agent_comms.native_attestation import ObservedAttestation
from agent_comms.native_custody import PiSessionChild
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.pi_payloads import StateData
from agent_comms.pi_rpc import PiRpcChannel


@dataclass(frozen=True)
class RecordedNativeCheckpoint:
    """References to original measured evidence, never a replacement checkpoint."""

    journal: Annotated[Path, PathText]
    reference: SummaryOperationIdentity
    commit_id: str

    def capture(self, session: NativeSessionIdentity, evidence: NativeEvidenceRead):
        """Borrow original records after the journal/native owners corroborate them."""
        session.require_session(self.reference.session_file)
        evidence.require_path(Path(session.session_file))
        header, entries = evidence.observe()
        session.require_same_session(NativeSessionIdentity(header.id, str(evidence.source.path)))

        def read(db):
            attempt = SelectedSummaryAttempt.one(db, operation_id=self.reference.operation_id)
            if attempt is None:
                raise ValueError("Recorded checkpoint has no original selected request")
            attempt.require_session(session.session_file)
            operation = CompactionOperation.one(db, commit_id=self.commit_id)
            if operation is None:
                raise ValueError("Recorded checkpoint has no original commit")
            # Read-only linkage verifies the original intent/source digest. It
            # does not obtain or consume the commit owner's returned ACK.
            operation.require_summary_link(attempt, admit_original=True)
            original = operation.committed_outcome()
            entry, = (row for row in entries if row.id == original.entry_id)
            if not isinstance(entry, ManagedCompactionEntry):
                raise ValueError("Recorded checkpoint requires an original managed native compaction")
            branch = evidence.branch(entry.id, entries)
            covered = operation.covered_prefix(entry, evidence, branch)
            attempt.request.retained.require_summary(entry.summary)
            return attempt, entry, covered

        original = CompactionJournal.observe_readonly(self.journal, read, absent=None)
        if original is None:
            raise ValueError("Recorded checkpoint has no original compaction journal")
        return original

    def _report(self, attempt: SelectedSummaryAttempt, entry: ManagedCompactionEntry,
               covered: frozenset[str]):
        # Membership comes from the original declared fact family. These counts
        # describe the corroborated envelope, not inferred summary prose.
        membership = Counter(fact.declared_name for fact in attempt.request.retained.facts)
        return {
            "reference": FieldCodec.encode(attempt.identity),
            "commit_id": self.commit_id,
            "native_entry_id": entry.id,
            "retained_facts": FieldCodec.encode(attempt.request.retained),
            "selected_model": FieldCodec.encode(attempt.request.selected),
            "settings": FieldCodec.encode(attempt.request.settings),
            "revision_mass": {
                "evaluated": False,
                "reason": "An original scope and authorized correction evidence are required, not content differences",
            },
            "canonical_availability": {
                "scope": "exact original selected-source envelope in its corroborated native commit",
                "evaluated": bool(attempt.request.retained.facts),
                "reason": "An empty retained envelope has no eligible fact denominator"
                          if not attempt.request.retained.facts else "Original envelope verified against committed payload",
                "source_digest": attempt.request.retained.source_digest.value,
                "required": len(attempt.request.retained.facts),
                "available": len(attempt.request.retained.facts),
                "by_fact": {kind: {"required": count, "available": count}
                            for kind, count in sorted(membership.items())},
                "covered_native_entries": len(covered),
            },
        }

    def observe(self, session: NativeSessionIdentity, evidence: NativeEvidenceRead):
        return self._report(*self.capture(session, evidence))

    def inspect(self, previous: RecordedNativeCheckpoint | None = None):
        """Read a checkpoint or adjacent-cut difference without a new model input.

        The optional previous reference is external evaluation input. It neither
        selects a live source nor carries native lifecycle/admission state.
        """
        with NativeEntry.open_evidence(Path(self.reference.session_file)) as evidence:
            header, _ = evidence.observe()
            session = NativeSessionIdentity(header.id, str(evidence.source.path))
            current_attempt, current_entry, covered = self.capture(session, evidence)
            report = self._report(current_attempt, current_entry, covered)
            if previous is not None:
                previous_attempt, previous_entry, _ = previous.capture(session, evidence)
                branch = evidence.branch(current_entry.id, evidence.entries)
                if previous_entry.id == current_entry.id or previous_entry not in branch:
                    raise ValueError("Checkpoint comparison requires distinct original ancestor cuts")
                report["source_changes"] = {
                    "previous": FieldCodec.encode(previous_attempt.identity),
                    "current": FieldCodec.encode(current_attempt.identity),
                    **current_attempt.request.retained.changed_from(previous_attempt.request.retained),
                }
            return report


@dataclass(frozen=True)
class RecordedNativeProbe:
    """One recorded tool-free probe, read through the original native owners."""

    session: NativeSessionIdentity
    input_id: Annotated[str, NativeInputIdText]
    answer_entry_id: str
    # Full-context controls have no compaction checkpoint. This is an explicit
    # external measurement field, not a nullable native lifecycle state.
    checkpoint: RecordedNativeCheckpoint | None = None

    def observe(self):
        with NativeEntry.open_evidence(Path(self.session.session_file)) as evidence:
            context = NativeContextProof.read_evidence(
                Path(self.session.session_file), self.input_id, evidence=evidence
            )
            self.session.require_same_session(NativeSessionIdentity(
                context.session_id, str(context.session_file)
            ))
            _, entries = evidence.observe()
            user, = (row for row in entries if row.id == context.session_entry_id)
            answer, = (row for row in entries if row.id == self.answer_entry_id)
            if not isinstance(answer, MessageEntry) or not answer.final_reply:
                raise ValueError("Recorded recall answer is not a successful native terminal")
            if answer.parent_id != user.id:
                raise ValueError("Recall requires a direct tool-free answer to its original probe")
            if self.checkpoint is not None:
                checkpoint = self.checkpoint.observe(self.session, evidence)
                if user.parent_id != checkpoint["native_entry_id"]:
                    raise ValueError("Recorded probe must immediately follow its original checkpoint")
            else:
                checkpoint = {
                    "applicable": False, "reason": "No compaction checkpoint declared for this control",
                    "canonical_availability": {
                        "evaluated": False, "reason": "Full-context control has no committed retained envelope"
                    },
                }
            return {
                # The located proof's Path is an acquired resource coordinate.
                # Export its original declared wire facts, not a second proof.
                "context": FieldCodec.encode({
                    item.metadata.get("wire_name", item.name): getattr(context, item.name)
                    for item in fields(NativeContextRecord)
                }),
                "session": FieldCodec.encode(self.session),
                "prompt": user.message.text,
                "answer": FieldCodec.encode(answer),
                "answer_text": answer.message.authoritative_text,
                "checkpoint": checkpoint,
                "canonical_availability": checkpoint["canonical_availability"],
                "prompt_scope": "original native user and assembled-context proof, not final provider payload",
            }


def retained_native_host(
    proc, launch, identity: NativeSessionIdentity, *, reader=None, stderr_task=None
):
    child = PiSessionChild(
        proc,
        reader if reader is not None else PiRpcChannel(proc.stdout),
        (
            stderr_task
            if stderr_task is not None
            else asyncio.create_task(PiSessionChild.stderr_tail(proc.stderr))
        ),
        (launch, (0, 0)),
        ObservedAttestation(
            StateData(session_id=identity.session_id, session_file=identity.session_file)
        ),
    )
    persistent = PersistentPiSession()
    assert persistent.retain(child, identity)
    return persistent
