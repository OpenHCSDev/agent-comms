"""Provider-free OLD installed writer seed for the one-shot operation gate."""
from dataclasses import replace
import json
from pathlib import Path
import sys

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message, MessageType
from agent_comms.threads import Thread


def main():
    root = Path(sys.argv[1])
    service = Comms(root)
    for name in ('original_sender', 'alpha', 'beta'):
        service.registry.declare(Thread(
            name, frozenset({'team'}) if name != 'original_sender' else frozenset(),
            str(root.parent), process_identity=ProcessIdentity.capture(__import__('os').getpid()),
        ))
    root_id = service.messaging.initialize_private_initial_protocol()
    original = service.bus.publisher.publish_initial_cohort(
        Message('original_sender', '#team', 'Retained original frozen audience', MessageType.INFO)
    )
    sender = service.registry.require('original_sender')
    service.registry.rename('original_sender', 'renamed_sender')
    service.registry.register(replace(service.registry.require('beta'), tags=frozenset({'other'})))
    print(json.dumps({'root_id': root_id, 'sender': FieldCodec.encode(sender),
                      'original': original.to_wire()}), flush=True)


if __name__ == '__main__':
    main()
