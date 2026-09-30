"""Inspect each installed writer's own declared schema; no index is read."""
import hashlib
import json

from agent_comms.private_bus_checkpoint import CheckpointTable


def declared_schema_digest():
    schema = {name: sql for table in CheckpointTable.__subclasses__()
              for name, sql in table.schema_objects().items()}
    return hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()


if __name__ == '__main__':
    print(declared_schema_digest(), flush=True)
