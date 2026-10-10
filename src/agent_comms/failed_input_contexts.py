"""One-time repair: record the model context of inputs that failed before Core kept it.

Before the terminal-failure settlement recorded the context, a failed native
input kept a row without one, and compaction of its session refused forever.
A row is repaired only when three independent records agree:

1. Core's failure diagnostic for that input names a native terminal failure
   for its thread (written only after Core verified the live context).
2. Pi's context journal, read through Core's proof reader, gives the context
   of the input's first request, in the row's own session.
3. The session file shows that input followed directly by a failed assistant
   message.

Full rows additionally require their execution to be failed by that same
native terminal failure. Triage claims still deferred by the old failure path
become Failed. Nothing is sent again.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .coordination_tables.executions import ExecutionRecord
from .coordinator import Coordination
from .diagnostics import NativeTerminalFailureRecord
from .native_entries import MessageEntry, NativeEntry
from .native_input_record import FullNativeExecution, TriageNativeExecution
from .native_pi import NativeContextJournal, NativeContextProof, NativePiUnavailable
from .native_runtime_input import NativeRuntimeInput
from .private_send_stage import TriageNativeSend


class FailedInputNotProven(ValueError):
    """One of the three records is missing or disagrees; the row is left unchanged."""


@dataclass(frozen=True)
class FailedInputContext:
    row: NativeRuntimeInput
    context: NativeContextProof

    @classmethod
    def prove(cls, root: Path, row: NativeRuntimeInput) -> FailedInputContext:
        cls.require_failure_diagnostic(root, row)
        try:
            return cls.read_native_records(row)
        except (NativePiUnavailable, ValueError) as error:
            # Pi's journal or the session disagrees with the row.
            raise FailedInputNotProven(str(error)) from error

    @classmethod
    def read_native_records(cls, row: NativeRuntimeInput) -> FailedInputContext:
        session = row.require_session_identity()
        with NativeContextJournal.open_evidence(session.path) as journal:
            generation = NativeContextJournal.first_generation(journal, row.input_id)
        if generation is None:
            raise FailedInputNotProven("Pi recorded no context for this input")
        with NativeEntry.open_evidence(session.path) as evidence:
            context = NativeContextProof.read_evidence(
                session.path, row.input_id, request_generation=generation, evidence=evidence
            )
            session.require_context(context)
            _header, entries = evidence.observe()
            position = next(
                index for index, entry in enumerate(entries) if entry.id == context.session_entry_id
            )
            following = entries[position + 1 : position + 2]
            if not following or not isinstance(following[0], MessageEntry):
                raise FailedInputNotProven("Input is not followed by an assistant message")
            following[0].require_failed_terminal(context.session_entry_id)
        return cls(row, context)

    @staticmethod
    def require_failure_diagnostic(root: Path, row: NativeRuntimeInput) -> None:
        try:
            record = NativeTerminalFailureRecord.read(root, row.input_id)
        except FileNotFoundError as error:
            raise FailedInputNotProven("Core recorded no failure for this input") from error
        except (TypeError, ValueError) as error:
            raise FailedInputNotProven(f"Core's failure record does not apply: {error}") from error
        if record.thread != row.owner_thread:
            raise FailedInputNotProven("Core's failure record names another thread")

    def record(self, store: Coordination) -> str:
        """Write the context and fail deferred claims in one coordinator transaction."""
        with store.session.transaction() as db:
            row = NativeRuntimeInput.one(db, input_id=self.row.input_id)
            if row != self.row:
                raise FailedInputNotProven("Input row changed while its records were read")
            execution = row.execution
            if isinstance(execution, FullNativeExecution):
                record = ExecutionRecord.one(db, execution_id=execution.execution_id)
                if not record.failed_by_native_terminal_failure():
                    raise FailedInputNotProven("Full input's execution is not a native terminal failure")
            row.commit_context(db, self.context)
            if isinstance(execution, TriageNativeExecution):
                claims = tuple(
                    store.assignments.get(assignment_id)
                    for assignment_id in execution.source_assignment_ids(db, row.input_id)
                )
                TriageNativeSend(claims).fail_claims(store, db)
                return f"recorded context; {len(claims)} triage claims failed"
            return "recorded context"


@dataclass(frozen=True)
class FailedInputReport:
    thread: str
    input_id: str
    stage: str
    result: str
    request_generation: int | None = None


def record_failed_input_contexts(root: Path, *, apply: bool) -> dict:
    """Report, and with ``apply`` repair, every sent input that has no recorded context."""
    with Coordination(str(root / "coordination.sqlite3")) as store:
        with store.session.read():
            rows = NativeRuntimeInput.select(
                store.session._connection,
                where="session_id IS NOT NULL AND session_entry_id IS NULL",
                parameters=(),
            )
        reports = []
        for row in rows:
            identity = (row.owner_thread, row.input_id, type(row.execution).declared_name)
            try:
                proved = FailedInputContext.prove(root, row)
                result = proved.record(store) if apply else "proven"
                reports.append(FailedInputReport(
                    *identity, result, proved.context.request_generation
                ))
            except FailedInputNotProven as error:
                reports.append(FailedInputReport(*identity, f"unchanged: {error}"))
    return {"applied": apply, "inputs": [asdict(report) for report in reports]}
