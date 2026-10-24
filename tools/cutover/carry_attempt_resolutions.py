"""Carry retired resolution text into the attempt's declared family, then delete this step."""

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import ClassVar

from agent_comms.attempt_resolution import (
    AttemptResolution,
    FailedTurnResolution,
    OperatorStopped,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_attempts import AttemptRecord, GoalAttemptStore, Reservation
from agent_comms.goal_failure_observation import FailedTurnEvidence
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.turn_failure import BackendFailed
from agent_comms.typed_table import TypedRow


@dataclass(frozen=True)
class StoredResolution(TypedRow):
    key: str
    resolution: str

    def carried(self, db: Connection) -> AttemptResolution | None:
        if self.resolution == "Operator stopped goal turn.":
            return OperatorStopped()
        if self.resolution == "backend_failed":
            return FailedTurnResolution(BackendFailed)
        if self.resolution == "Goal turn ended without verified terminal progress.":
            # 557f86433^ turn_terminal.py:90-92 wrote one text for every failure.
            # The evidence retained alongside that attempt names the actual member.
            reservation = FieldCodec.decode(Reservation, FieldCodec.parse(self.key))
            evidence = FailedTurnEvidence.require(db, attempt_id=reservation.attempt_id)
            return FailedTurnResolution(evidence.failure)
        return None


class AttemptResolutions(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (GoalAttemptStore,)

    def carry(self, *, apply: bool) -> Carried:
        column, = AttemptRecord._fields_named(("resolution",))
        counts: Counter[str] = Counter()
        rewritten: list[Path] = []
        changed = 0
        for path in self.input_files():
            with closing(GoalAttemptStore.read_snapshot(path)) as db:
                if not AttemptRecord.present(db):
                    continue
                legacy = tuple(StoredResolution.read(db.execute(AttemptRecord._sql(
                    f'SELECT {{reservation}} AS key,{{resolution}} '
                    f'FROM "{AttemptRecord.declared_name}" '
                    'WHERE {resolution} IS NOT NULL AND CASE '
                    "WHEN json_valid({resolution}) THEN json_type({resolution})!='object' "
                    'ELSE 1 END'
                ))))
                updates: list[tuple[object, object]] = []
                for row in legacy:
                    resolution = row.carried(db)
                    label = type(resolution).declared_name if resolution is not None else "nulled"
                    counts[label] += 1
                    updates.append((column.encode(resolution), row.key))
            changed += len(updates)
            if apply and updates:
                with (
                    closing(GoalAttemptStore.writer(path)) as db,
                    GoalAttemptStore.write_transaction(db),
                ):
                    self.rewrite_stored(db, AttemptRecord, column, updates)
                rewritten.append(path.relative_to(self.root))
        return Carried(changed, detail=dict(counts), rewritten=tuple(rewritten))
