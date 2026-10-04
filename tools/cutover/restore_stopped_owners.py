"""Original-format explicit recovery of the SAME acquired stopped batch.

The operation certifies original bytes before this child. Credentials cross its
existing private stdin pipe only, and the original wire OFD stays held by its
parent throughout. No fence, installation, input or provider is repeated.
"""
import json
from pathlib import Path
import sys

from phased_owner_kernel import load_original_phase


def main():
    package, descriptor = sys.argv[1:]
    load_original_phase(Path(package))
    from agent_comms.comms import wire
    from agent_comms.field_codec import FieldCodec
    from agent_comms.owner_restart import OwnerRestartHandoff, StoppedOwnerBatch

    handoff = FieldCodec.decode(OwnerRestartHandoff, json.load(sys.stdin))
    service = wire(Path(handoff.root))
    stopped = StoppedOwnerBatch.accept(service.owners, int(descriptor), handoff)
    results = stopped.handoff.restore(stopped.lifecycle)
    print(json.dumps(FieldCodec.encode(results)), flush=True)


if __name__ == '__main__':
    main()
