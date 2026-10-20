"""Give bridge state files their declared indexes once; keep every row.

Bridges written before the sequence index was declared hold delivery_0_idx on
outcome, a name the declaration now gives the sequence index. Their delivery
rows are the only record of attempted inputs, so the file is converted in
place, never moved aside. Delete once laptop and desktop bridge state is carried.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from agent_comms.cli_bridges.shared import BridgeStateTable
from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.locked_store import SQLiteFile
from agent_comms.typed_table import SQLiteTableName, TypedRow


@dataclass(frozen=True)
class StoredObject(TypedRow):
    """One catalog entry of the old file: SQLite's own type, name and table."""

    type: str
    name: str
    tbl_name: str
    sql: str

    def drop(self, db) -> None:
        db.execute(f'DROP {self.type.upper()} "{self.name}"')

    @classmethod
    def stored(cls, db) -> list[StoredObject]:
        return cls.read(db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name"))


@dataclass(frozen=True)
class RowCount(TypedRow):
    rows: int


def row_counts(db) -> dict[str, int]:
    return {table.name: RowCount.read(db.execute(f'SELECT COUNT(*) AS rows FROM "{table.name}"'))[0].rows
            for table in SQLiteTableName.stored(db)}


def carry(state: Path) -> None:
    path = state / BridgeStateTable.filename
    if not path.is_file():
        raise RelationViolationError(f'{path} does not exist; nothing to carry')
    declared = BridgeStateTable.declared_schema()
    tables = {row.declared_name for row in BridgeStateTable.schema_rows()}
    with closing(CoordinationStore.writer(path, isolation_level=None)) as db:
        before, counts = StoredObject.stored(db), row_counts(db)
        with SQLiteFile.write_transaction(db):
            for item in StoredObject.stored(db):
                if item.type == 'table':
                    if item.name not in tables:
                        raise RelationViolationError(f'{path}: undeclared table {item.name} holds rows')
                    if item.sql != declared[item.name]:
                        raise RelationViolationError(f'{path}: table {item.name} columns differ')
                elif declared.get(item.name) != item.sql:
                    item.drop(db)
            for row in BridgeStateTable.schema_rows():
                row.create(db, if_not_exists=True)
            BridgeStateTable.require_schema(db)
            if row_counts(db) != counts:
                raise RelationViolationError(f'{path}: row counts changed')
        after = StoredObject.stored(db)
    for label, objects in (('before', before), ('after', after)):
        print(f'{path} {label}:')
        for item in objects:
            print(f'  {item.type} {item.name} on {item.tbl_name}: {item.sql}')
    print(f'{path} rows kept: {counts}')


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('states', nargs='+', type=Path, help='bridge state directories')
    for state in parser.parse_args(argv).states:
        carry(state)


if __name__ == '__main__':
    main(sys.argv[1:])
