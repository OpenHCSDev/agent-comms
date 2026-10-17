"""Run under the selected current runtime; emit one typed acquired read.

No registry or guard write, process signal, native request or input claim_scope.
The current registry owner validates the document under its shared lock. Old
records must be carried once before this reader can consume them.
"""
import json
from pathlib import Path
import sys

from agent_comms.field_codec import FieldCodec
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
    print(json.dumps(FieldCodec.encode(packet)), flush=True)


if __name__ == '__main__':
    main()
