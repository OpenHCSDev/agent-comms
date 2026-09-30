"""Target-only completion under ORIGINAL stopped batch and route-directory OFDs."""
import json
from pathlib import Path
import sys

from agent_comms.active_route import ActiveRoute, _publish_active_route_locked, read_active_route
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_restart import OwnerRestartHandoff, StoppedOwnerBatch
from agent_comms.store_files import _atomic_write_text


def main():
    wire_descriptor, route_descriptor = map(int, sys.argv[1:])
    packet = json.load(sys.stdin)
    handoff = FieldCodec.decode(OwnerRestartHandoff, packet['handoff'])
    original = ActiveRoute.from_record(packet['original_route'])
    target = ActiveRoute.from_record(packet['target_route'])
    path, receipt = Path(packet['route_path']), Path(packet['receipt'])
    service = Comms(Path(handoff.root))
    stopped = StoppedOwnerBatch.accept(service.owners, wire_descriptor, handoff)
    # Validate EVERY original process/admission witness before publication.
    snapshot = service.registry.snapshot()
    for owner in handoff.owners:
        owner.require_current(snapshot)
    service.owners.pin_private_nk_launch(target.root, target.wire_root_id, target.native_package)
    _publish_active_route_locked(target, path, route_descriptor, expected=original)
    assert read_active_route(path) == target
    proof = json.loads(receipt.read_text())
    proof['phase'] = 'target_route_published'
    _atomic_write_text(receipt, json.dumps(proof, indent=2), fsync_parent=True)
    results = stopped.launch()
    proof.update(phase='target_owners_launched', owners=len(results),
                 route_published_before_first_launch=True,
                 original_wire_descriptor_retained=True)
    _atomic_write_text(receipt, json.dumps(proof, indent=2), fsync_parent=True)
    print(json.dumps(FieldCodec.encode(results)), flush=True)


if __name__ == '__main__':
    main()
