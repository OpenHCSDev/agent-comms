"""Goal wait owners (P236).

Old rows named their owner by creation date only; each is resolved once to the
owner's recorded incarnation. Only goal_waits.json changes; registry, sessions
and inputs are never touched.
"""
from __future__ import annotations

import json
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.goal_waits import GoalWaits, RecordedGoalWait
from agent_comms.registration import Registration
from agent_comms.registry_document import RegistrySnapshot

from cutover.carry import Carried, Carry


def carry_rows(raw: dict, snapshot: RegistrySnapshot) -> dict[str, RecordedGoalWait]:
    """Resolve old date-only rows once at cutover, never on the runtime path.
    A row that already names its owner is kept as it is."""
    pending = {goal: row for goal, row in raw.items() if "owner" not in row}
    carried = {goal: FieldCodec.decode(RecordedGoalWait, row)
               for goal, row in raw.items() if "owner" in row}
    for thread in snapshot.threads.values():
        goal = thread.goal.reported_id()
        if goal not in pending:
            continue
        row = dict(pending.pop(goal))
        if row.pop("owner_created_at") != thread.created_at or row["goal_id"] != goal:
            raise ValueError(f"Wait {goal} does not belong to its recorded owner")
        row["owner"] = thread.incarnation.require_recorded().to_wire()
        carried[goal] = FieldCodec.decode(RecordedGoalWait, row)
    if pending:
        raise ValueError(f"Unresolved wait owners; nothing published: {sorted(pending)}")
    return carried


class GoalWaitOwners(Carry):
    """Each recorded goal wait names its owner's incarnation."""

    after = "carry_goal_publications"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        waits = GoalWaits.at(root)
        raw = json.loads(waits.path.read_text()) if waits.path.exists() else {}
        changed = sum("owner" not in row for row in raw.values())
        carried = carry_rows(raw, Registration(root / "registry.json").snapshot())
        if apply and changed:
            waits.replace(carried)
        return Carried(changed, {"waits": len(carried)}, (waits.path.name,) if changed else ())


if __name__ == "__main__":
    GoalWaitOwners.main()
