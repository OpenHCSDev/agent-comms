"""Original installed publisher creates private declarations; zero providers."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.threads import Thread
from agent_comms.tools import invoke_tool


def main():
    root = Path(sys.argv[1])
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    service = Comms(root)
    root_id = service.messaging.initialize_private_initial_protocol()
    beta = service.registry.declare(Thread('beta', frozenset({'team'}), str(root)))
    alpha = service.registry.declare(Thread('alpha', frozenset({'team'}), str(root),
                                           process_identity=ProcessIdentity.capture(os.getpid())))
    alpha, admission = service.registry.live_owner_with_admission(alpha.name)
    alpha, _ = service.registry.lease_live_turn_with_admission(
        alpha, 'original-task-turn', expected_generation=admission)
    os.environ['PI_AGENT_ID'] = alpha.name
    result = invoke_tool(service, 'comms_decision', {
        'chosen': 'ORIGINAL λ PATH\nNever replay UNKNOWN.',
        'rejected': ['Do not substitute another original.'], 'to': beta.name,
    })
    service.registry.register(replace(alpha, process_identity=None, active_turn=None))
    # Opaque fixture bytes exercise the write-set fence. This is NOT a native
    # UNKNOWN input/journal witness and must never be reported as one.
    protected = root / 'protected-fixture-source.proof'
    protected.write_bytes(b'Protected fixture source; no writes\n')
    protected.chmod(0o600)
    print(json.dumps({'root_id': root_id, 'original': result['reference']}), flush=True)


if __name__ == '__main__':
    main()
