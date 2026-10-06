"""Strict target declarations validate the complete one-shot projection in RAM."""
import json
import sys

from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.goal_history import GoalHistoryEntry, GoalHistoryStore
from agent_comms.owner_lifecycle import OwnerReleaseReceipt
from agent_comms.registry_document import RegistryDocument


def main():
    packet = json.load(sys.stdin)
    registry = RegistryDocument.from_wire(packet['registry'])
    releases = FieldCodec.decode(dict[str, OwnerReleaseReceipt], packet['releases'])
    history = FieldCodec.decode(tuple[GoalHistoryEntry, ...], packet['goal_history'])
    if history != GoalHistoryStore.acquire_read_only(Path(sys.argv[1])):
        raise ValueError('Target goal history differs from the acquired original rows')
    for thread in registry.threads.values():
        thread.require_idle()
    for receipt in releases.values():
        receipt.thread.require_idle()
    print(json.dumps({'threads': len(registry.threads), 'releases': len(releases), 'goal_history_rows': len(history)}), flush=True)


if __name__ == '__main__':
    main()
