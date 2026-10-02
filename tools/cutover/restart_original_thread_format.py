"""Authentic original runtime executes the ONE declared retained batch kernel."""
import json
from pathlib import Path
import sys

from phased_owner_kernel import load_original_phase


def main():
    package, route_descriptor = sys.argv[1:]
    load_original_phase(Path(package))
    from agent_comms.comms import Comms
    from agent_comms.field_codec import FieldCodec
    from agent_comms.owner_restart import OwnerRestartRequest
    from agent_comms.owner_launch import RestartEnvironment
    from thread_retirement_cutover import ThreadRetirementCutover

    packet = json.load(sys.stdin)
    operation = ThreadRetirementCutover(
        Path(packet['target_python']), packet['target_environment'],
        Path(packet['route_path']), int(route_descriptor), packet['original_route'],
        packet['target_route'], Path(packet['receipt']),
    )
    service = Comms(Path(packet['original_route']['root']))
    # Original guarded reader and the declared target boundary prevalidate both
    # carriers before any admission fence or process signal.
    with service.registry.store.reading() as registry, service.owners.releases.reading() as releases:
        operation.validate(registry, releases)
    request = OwnerRestartRequest(
        agent_bin=packet['target_binary'],
        runtime=FieldCodec.decode(RestartEnvironment, packet['runtime']),
        source_interpreter=sys.executable,
    )
    results = operation.restart(service.owners, request)
    print(json.dumps(FieldCodec.encode(results)), flush=True)


if __name__ == '__main__':
    main()
