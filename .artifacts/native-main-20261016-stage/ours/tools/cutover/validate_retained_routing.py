"""Target-format validation under the inherited original writer, before writes."""
from contextlib import closing, nullcontext
import json
from pathlib import Path
import sqlite3
import sys

from agent_comms.field_codec import FieldCodec
from install_retained_index import require_retained_writer
from routing_carry import RoutingCarryPlan, file_witness


def read_plan(root, descriptor, root_id):
    root = Path(root)
    require_retained_writer(root, descriptor, root_id)
    plan = FieldCodec.decode(RoutingCarryPlan, json.load(sys.stdin))
    if plan.root_id != root_id:
        raise ValueError('Prepared routing plan belongs to another certified root.')
    database = root / 'transcript_routes.sqlite3'
    if file_witness(database) != plan.database:
        raise ValueError('Original annotation database changed before carry.')
    return root, plan


def open_original(database):
    return (closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True))
            if database.exists() else nullcontext(None))


def main():
    root, plan = read_plan(*sys.argv[1:])
    with open_original(root / 'transcript_routes.sqlite3') as connection:
        plan.require_target(root, connection)
    print(json.dumps({'prevalidated_cells': len(plan.cells),
                      'prevalidated_turns': len(plan.turns)}), flush=True)


if __name__ == '__main__':
    main()
