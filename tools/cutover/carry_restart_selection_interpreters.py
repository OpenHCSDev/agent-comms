"""Move retained restart interpreters onto their owning selections once."""

import json
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.restart_queue import DIRECTORY, QueuedRestart
from agent_comms.store_files import _atomic_write_text

from .carry import Carried, Carry


class RestartSelectionInterpreters(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return tuple(sorted((root / DIRECTORY).rglob("*.json")))

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        rewritten = []
        for path in cls.backup_files(root):
            payload = json.loads(path.read_text())
            if "source_interpreter" not in payload:
                continue
            selection = payload["selection"]
            if "interpreter" in selection:
                raise ValueError("Restart record has two interpreter owners")
            selection["interpreter"] = payload.pop("source_interpreter")
            FieldCodec.decode(QueuedRestart, payload)
            rewritten.append(str(path.relative_to(root)))
            if apply:
                _atomic_write_text(path, json.dumps(payload, sort_keys=True), fsync_parent=True)
        return Carried(len(rewritten), rewritten=tuple(rewritten) if apply else ())


if __name__ == "__main__":
    RestartSelectionInterpreters.main()
