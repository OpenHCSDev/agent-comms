"""Strict target declarations validate the complete one-shot projection in RAM."""
import json
import sys

from agent_comms.field_codec import FieldCodec
from agent_comms.owner_lifecycle import OwnerReleaseReceipt
from agent_comms.registry_document import RegistryDocument


def main():
    packet = json.load(sys.stdin)
    registry = RegistryDocument.from_wire(packet['registry'])
    releases = FieldCodec.decode(dict[str, OwnerReleaseReceipt], packet['releases'])
    for thread in registry.threads.values():
        thread.require_idle()
    for receipt in releases.values():
        receipt.thread.require_idle()
    print(json.dumps({'threads': len(registry.threads), 'releases': len(releases)}), flush=True)


if __name__ == '__main__':
    main()
