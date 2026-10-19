"""Carry nullable wait-report metadata into its declared idle/running member."""

from contextlib import closing
import json
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec
from agent_comms.store_files import _atomic_write_text
from agent_comms.goal_waits import (
    GoalWaits,
    IdleReportTurn,
    RecordedGoalWait,
    ReportTurn,
    RunningReportTurn,
    StoredGoalWaitRow,
)
from .carry import Carried, StoreCarry
from .carry_goal_wait_targets import GoalWaitTargets


class GoalWaitReports(StoreCarry):
    store = GoalWaits

    @classmethod
    def precedes(cls, later):
        return issubclass(later, GoalWaitTargets)

    @classmethod
    def backup_files(cls, root):
        return (root / "goal_waits.json", cls.store.path_in(root))

    @staticmethod
    def converted(value):
        if "report_turn" in value:
            return None
        value = dict(value)
        turn_id = value.pop("report_turn_id")
        generation = value.pop("report_turn_generation")
        value.pop("target_turn_generations")  # No production consumer of this stored copy.
        if (turn_id is None) != (generation is None):
            raise ValueError("Reporting turn ID and generation disagree")
        report = IdleReportTurn() if turn_id is None else RunningReportTurn(turn_id, generation)
        value["report_turn"] = FieldCodec.encode(report, ReportTurn)
        return FieldCodec.decode(RecordedGoalWait, value)

    @classmethod
    def carry(cls, root: Path, *, apply: bool):
        changed = 0
        rewritten = []
        previous = root / "goal_waits.json"
        if previous.exists():
            rows = FieldCodec.parse(previous.read_bytes())
            carried = {key: cls.converted(value) for key, value in rows.items()}
            for key, value in carried.items():
                if value is not None:
                    rows[key] = FieldCodec.encode(value)
                    changed += 1
            if apply and any(value is not None for value in carried.values()):
                _atomic_write_text(previous, json.dumps(rows))
                rewritten.append(previous.name)
        path = cls.store.path_in(root)
        if path.exists():
            with CoordinationStore.observing(path) as db:
                rows = tuple(
                    (key, cls.converted(FieldCodec.parse(payload)))
                    for key, payload in db.execute(
                        f"SELECT goal_id, wait FROM {StoredGoalWaitRow.declared_name}"
                    )
                )
            updates = tuple(
                StoredGoalWaitRow(key, value) for key, value in rows if value is not None
            )
            changed += len(updates)
            if apply and updates:
                with closing(CoordinationStore.writer(path)) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    for row in updates:
                        row.upsert(db)
                rewritten.append(path.name)
        return Carried(changed, rewritten=tuple(rewritten))


if __name__ == "__main__":
    GoalWaitReports.main()
