"""Enroll an already running native test host in the actual retained custody owner."""

import asyncio
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Annotated

from agent_comms.compaction_identity import SummaryOperationIdentity
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import CompactionOperation, SelectedSummaryAttempt
from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.native_entries import CompactionEntry, MessageEntry, NativeEntry
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

    def observe(self, session: NativeSessionIdentity, entries, probe):
        session.require_session(self.reference.session_file)

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
            if not isinstance(entry, CompactionEntry):
                raise ValueError("Recorded checkpoint is not an original native compaction")
            if probe.parent_id != entry.id:
                raise ValueError("Recorded probe must immediately follow its original checkpoint")
            attempt.request.retained.require_summary(entry.summary)
            return {
                "reference": FieldCodec.encode(attempt.identity),
                "commit_id": operation.commit_id,
                "native_entry_id": original.entry_id,
                "retained_facts": FieldCodec.encode(attempt.request.retained),
                "selected_model": FieldCodec.encode(attempt.request.selected),
                "settings": FieldCodec.encode(attempt.request.settings),
            }

        original = CompactionJournal.observe_readonly(self.journal, read, absent=None)
        if original is None:
            raise ValueError("Recorded checkpoint has no original compaction journal")
        return original


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
            checkpoint = self.checkpoint.observe(self.session, entries, user) if self.checkpoint else {
                "applicable": False, "reason": "No compaction checkpoint declared for this control"
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
