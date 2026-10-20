"""Reset settled runtime restart records; never convert or replay requests."""

import json
from pathlib import Path

from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.restart_queue import DIRECTORY, RestartQueue, RestartState
from agent_comms.thread_identity import OwnerIdentity

from .carry import Carried, Carry


class RestartSelectionInterpreters(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return tuple(sorted((root / DIRECTORY).rglob("*.json")))

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        settled = []
        for path in cls.backup_files(root):
            payload = json.loads(path.read_text())
            state = FieldCodec.decode(RestartState, payload["state"])
            if state.active:
                owner = FieldCodec.decode(OwnerIdentity, payload["selection"]["identity"])
                try:
                    RestartQueue(root / DIRECTORY).read(path)
                except MalformedRecord as error:
                    raise ValueError(
                        f"Pending restart for {owner.incarnation.name} is incompatible: {error}"
                    ) from error
            else:
                settled.append(path)
        # Check every retained request before deleting any settled runtime state.
        if apply:
            for path in settled:
                path.unlink()
        return Carried(
            len(settled), {"settled_deleted": len(settled)},
            tuple(str(path.relative_to(root)) for path in settled) if apply else (),
        )


if __name__ == "__main__":
    RestartSelectionInterpreters.main()
