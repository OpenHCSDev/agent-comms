"""Carry inline active turns into their own records before a runtime cutover.

Run only after the installation owner has stopped old-format registry writers.
No session, input, execution or provider data is changed. Production has only the
new reader; this one-use migration belongs outside the installed implementation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore
from agent_comms.thread_identity import RecordedThreadIncarnation, TurnIdentity
from agent_comms.turn_lease import ActiveTurn, TurnRecord


def split_turn_records(root: Path) -> int:
    store = RegistryStore(root.resolve() / 'registry.json')
    with store.locked():
        store.private_guard_unlocked()
        raw = json.loads(store.path.read_text())
        changed = 0
        for name, thread in raw['threads'].items():
            if 'active_turn' not in thread:
                continue
            turn = thread.pop('active_turn')
            record = None
            if turn is not None:
                active = ActiveTurn.from_wire(turn)
                if active.turn_generation is None:
                    raise ValueError(f'{name}: an active turn without a generation needs owner review')
                identity = TurnIdentity(
                    RecordedThreadIncarnation(name, thread['created_at']), active.turn_generation,
                )
                record = TurnRecord.begin(store.path, identity, active)
            thread['turn_record'] = FieldCodec.encode(record)
            changed += 1
        if changed:
            store.save_unlocked(RegistryDocument.from_wire(raw))
        return changed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    print(split_turn_records(parser.parse_args().root))
