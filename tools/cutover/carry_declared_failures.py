"""Carry obsolete reason labels into declared failure classes and the failure key.

Only installation reads the retired spellings. Delete after every retained root
has been carried; running readers have no compatibility decoder.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import ClassVar

from agent_comms import turn_failure as failures
from agent_comms.diagnostics import DiagnosticDocument
from agent_comms.errors import RelationViolationError
from agent_comms.goal_attempts import AttemptRecord, GoalAttemptStore
from agent_comms.goal_failure_observation import FailedTurnEvidence, GoalLedgerTable
from agent_comms.historical_views import HistoryArchive
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import SQLiteFile
from agent_comms.store_files import AtomicFile
from agent_comms.typed_table import HeldBusFile, sql_literal

from .carry_bus_history import CarryCatalogSource
from .carry_claim_fields import ClaimFields
from .retired_claim_fields import RetiredClaimFields


class DeclaredFailures(RetiredClaimFields, Carry):
    opened_files = (HistoryArchive, GoalAttemptStore, DiagnosticDocument)
    # Both changes affect one table. Rebuild it once with both source columns;
    # ClaimFields then observes the already-carried SQL claim field.
    precedes = (ClaimFields,)

    old_names: ClassVar[dict[str, type[failures.TurnFailure]]] = {
        "backend_failed": failures.BackendFailed,
        "native_preflight_timeout": failures.NativePreflightTimeout,
        "native_preflight_exit": failures.NativePreflightExit,
        "pi_input_id_unavailable": failures.InputIdUnavailable,
        "session_identity_uncertain": failures.IdentityUncertain,
        "input_authority_changed": failures.AuthorityChanged,
        "unrecognized_followup_input": failures.FollowupUnrecognized,
        "current_prompt_input_missing": failures.InputMissing,
        "model_request_failed": failures.ModelRequestFailed,
        "assistant_final_stop_missing": failures.FinalStopMissing,
        "queued_input_start_missing": failures.QueuedInputMissing,
    }

    def roots(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys((
            self.root,
            *(source.mapped_root(self.root, self.original_root)
              for source in CarryCatalogSource.read(self.root, self.original_root)),
        )))


    def carry(self, *, apply: bool) -> Carried:
        self.input_files()
        if apply:
            # The installer's root hold also covers its retained, inactive snapshots.
            HeldBusFile(GoalAttemptStore, GoalAttemptStore.path_in(
                GoalAttemptStore.directory_in(self.root))).require()
        results = tuple(self.carry_root(root, apply=apply) for root in self.roots())
        return Carried(
            sum(result.changed for result in results),
            rewritten=tuple(name for result in results for name in result.rewritten),
        )

    def carry_root(self, root: Path, *, apply: bool) -> Carried:
        changed = 0
        rewritten: list[Path] = []
        store = GoalAttemptStore.directory_in(root)
        path = GoalAttemptStore.path_in(store)
        if path.exists():
            with closing(SQLiteFile.writer(":memory:")) as prepared:
                with closing(SQLiteFile.read_snapshot(path)) as original:
                    original.backup(prepared)
                columns = FailedTurnEvidence.stored_columns(prepared)
                replacements = self.retired_columns(FailedTurnEvidence, columns)
                if "reason" in columns:
                    for (reason,) in prepared.execute(
                        f'SELECT DISTINCT reason FROM "{FailedTurnEvidence.declared_name}"'
                    ):
                        if reason not in self.old_names:
                            raise RelationViolationError(f"Unknown stored failure at {path}: {reason!r}")
                    cases = " ".join(
                        f"WHEN {sql_literal(old)} THEN {sql_literal(kind)}"
                        for old, kind in self.old_names.items()
                    )
                    replacements["failure"] = f"CASE reason {cases} END"
                if replacements:
                    FailedTurnEvidence.rebuild(prepared, replacements)
                    changed += 1
                if AttemptRecord.stored_columns(prepared):
                    for old, kind in self.old_names.items():
                        if old != kind.declared_name:
                            changed += prepared.execute(
                                f'UPDATE "{AttemptRecord.declared_name}" SET resolution=? '
                                "WHERE resolution=? AND json_extract(phase,'$.kind')='failed'",
                                (kind.declared_name, old),
                            ).rowcount
                if changed:
                    GoalLedgerTable.require_schema(prepared)
                    if apply:
                        prepared.commit()
                        with closing(SQLiteFile.writer(path)) as destination:
                            prepared.backup(destination)
                        GoalAttemptStore.publish_change(path)
                        rewritten.extend(file.relative_to(self.root) for file in (
                            path, *GoalAttemptStore.source_paths(store),
                        ))
        for diagnostic in DiagnosticDocument.matching_in(root):
            document = json.loads(diagnostic.read_text())
            if "turn_id" not in document or "reason" not in document:
                continue
            if "failure" in document:
                raise RelationViolationError(f"Both failure formats are present at {diagnostic}")
            old = document.pop("reason")
            document["failure"] = self.old_names[old].declared_name
            if apply:
                AtomicFile(diagnostic).write_text(json.dumps(document, indent=2) + "\n")
                rewritten.append(diagnostic.relative_to(self.root))
            changed += 1
        return Carried(changed, rewritten=tuple(rewritten))
