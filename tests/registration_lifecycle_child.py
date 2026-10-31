"""Run the actual registry lifecycle in a fresh process and report its stored snapshots."""

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from agent_comms.child_process import ProcessIdentity
from agent_comms.nominal.field_codec import FieldCodec, TurnId
from agent_comms.registration import Registration
from agent_comms.repo import Checkout
from agent_comms.thread_text import ForkedParent, GivenThreadText
from agent_comms.threads import Thread


def main(root: Path, turn: TurnId) -> None:
    root.mkdir()
    registry = Registration(root / "registry.json")
    steps = []

    def capture() -> None:
        row = json.loads(registry.store.path.read_text().replace(str(root), "<root>"))
        row["turns"] = {name: FieldCodec.encode(thread.turn_record.read())
                        for name, thread in registry.snapshot().threads.items()}
        assert Registration(registry.store.path).snapshot() == registry.snapshot()
        steps.append(row)

    registry.register(Thread("owner", frozenset({"team"}), Checkout(root),
                             process_identity=ProcessIdentity.capture(os.getpid()), created_at=10.0))
    capture()
    registry.register(Thread("child", frozenset(), Checkout(root),
                             parent=ForkedParent("owner"), created_at=20.0))
    capture()
    registry.register(replace(registry.require("owner"), title=GivenThreadText("metadata")))
    capture()
    registry.lease_local_turn("owner", turn)
    capture()
    registry.release_turn(registry.require("owner").turn_lease)
    capture()
    registry.rename("owner", "renamed")
    capture()
    registry.unregister("renamed")
    capture()
    registry.archive("renamed")
    capture()
    registry.begin_delete("renamed")
    capture()
    with registry.store.editing() as edit:
        edit.document.remove_originals((edit.document.threads["renamed"],))
        edit.commit()
    capture()
    print(json.dumps(steps, sort_keys=True))


if __name__ == "__main__":
    main(Path(sys.argv[1]), TurnId(sys.argv[2]))
