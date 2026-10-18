"""One-time P236 wait carry; run with OLD wait readers/writers quiesced.

Use the new Core Python before starting new owners. Dry-run by default. This
only carries goal_waits.json; it never changes registry, sessions or inputs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.goal_waits import GoalWaits, RecordedGoalWait
from agent_comms.registration import Registration
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.store_files import _store_lock


def carry_rows(raw: dict, snapshot: RegistrySnapshot) -> dict[str, RecordedGoalWait]:
    """Resolve old date-only rows once at cutover, never on the runtime path."""
    pending = dict(raw)
    carried = {}
    for thread in snapshot.threads.values():
        goal = thread.goal
        if goal is None or goal.id not in pending:
            continue
        row = dict(pending.pop(goal.id))
        if row.pop("owner_created_at") != thread.created_at or row["goal_id"] != goal.id:
            raise ValueError(f"Wait {goal.id} does not belong to its recorded owner")
        row["owner"] = thread.incarnation.require_recorded().to_wire()
        carried[goal.id] = FieldCodec.decode(RecordedGoalWait, row)
    if pending:
        raise ValueError(f"Unresolved wait owners; nothing published: {sorted(pending)}")
    return carried


def carry_locked(root: Path, *, apply: bool = False) -> dict:
    """Carry under the caller's retained wire lock; use the existing wait writer."""
    waits = GoalWaits.at(root)
    registry = Registration(root / "registry.json")
    raw = json.loads(waits.path.read_text()) if waits.path.exists() else {}
    carried = carry_rows(raw, registry.snapshot())
    if apply:
        waits.replace(carried)
    return {"waits": len(carried), "applied": apply}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--apply", action="store_true", help="Publish only after old readers/writers stop")
    args = parser.parse_args()
    with _store_lock(args.root / "wire"):
        print(json.dumps(carry_locked(args.root, apply=args.apply)))


if __name__ == "__main__":
    main()
