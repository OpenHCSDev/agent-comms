"""Move canonical goal waits into their indexed owner; retire the old JSON rows."""

from pathlib import Path

from .carry import Carried, Carry

from agent_comms.field_codec import FieldCodec
from agent_comms.goal_waits import GoalWaits, RecordedGoalWait


class GoalWaitTargets(Carry):

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        previous = root / "goal_waits.json"
        if not previous.exists():
            return Carried(0)
        waits = FieldCodec.decode(
            dict[str, RecordedGoalWait], FieldCodec.parse(previous.read_bytes())
        )
        store = GoalWaits.at(root)
        if store.path.exists() and store.read() != waits:
            raise ValueError("Existing indexed waits differ from the saved JSON rows")
        if apply:
            store.record_many(waits.values())
            if store.read() != waits:
                raise ValueError("Indexed goal wait carry did not preserve the saved rows")
            previous.unlink()
        return Carried(
            len(waits), {"waits": len(waits)}, (previous.name, store.path.name) if apply else ()
        )


if __name__ == "__main__":
    GoalWaitTargets.main()
