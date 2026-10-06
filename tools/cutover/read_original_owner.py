"""Run ONLY under the authentic original runtime; emit one acquired read.

No registry or guard write, process signal, native request or input admission.
The existing registry owner validates the document under its shared lock. A
target Thread is never handed an original-format record.
"""
import json
from pathlib import Path
import sys

from agent_comms.registration import Registration
from owner_read_projection import OwnerReadProjection


def main():
    root, name, projection_name = sys.argv[1:]
    projection = OwnerReadProjection.decode(projection_name)
    registry_path = Path(root) / 'registry.json'
    if not registry_path.is_file() or not registry_path.with_name('.registry.json.lock').is_file():
        raise ValueError('Original registry and existing shared lock are required')
    expected = sys.stdin.read()
    registry = Registration(registry_path)
    packet = projection.read(registry, Path(root), name, json.loads(expected) if expected else None)
    print(json.dumps(packet), flush=True)


if __name__ == '__main__':
    main()
