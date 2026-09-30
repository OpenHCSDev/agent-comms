"""Read a protected preimage with its authentic installed Thread declaration."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

from agent_comms.field_codec import FieldCodec
from agent_comms.pi_vocabulary import OffThinkingLevel
from agent_comms.registry_document import RegistryDocument


def main():
    path, expected_digest = sys.argv[1:]
    names = json.loads(sys.stdin.read())
    descriptor = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        original = os.fstat(source.fileno())
        if not stat.S_ISREG(original.st_mode) or original.st_uid != os.geteuid():
            raise ValueError('Original settings preimage is not owned regular storage')
        if original.st_mode & 0o077:
            raise ValueError('Original settings preimage must be private')
        raw = source.read()
        if os.fstat(source.fileno()) != original:
            raise ValueError('Original settings preimage changed during observation')
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError('Original settings preimage differs from reviewed digest')
    snapshot = RegistryDocument.from_wire(json.loads(raw)).snapshot()
    selected = []
    for name in names:
        thread = snapshot.threads[name]
        if thread.thinking_level is not OffThinkingLevel:
            raise ValueError('Original selection was not OFF')
        selected.append({'incarnation': FieldCodec.encode(thread.incarnation),
                         'model': thread.model})
    # Only requested provenance/settings cross the pipe, never a full registry.
    print(json.dumps(selected))


if __name__ == '__main__':
    main()
