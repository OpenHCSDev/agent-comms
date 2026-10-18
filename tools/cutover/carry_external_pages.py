"""External item rows gain their earlier-page continuation (P427).

No saved items or positions are changed. Production readers accept only the
new declared schema.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.external_transcript import ExternalItemRow, ExternalTranscript, NoEarlierPage

from cutover.carry import Carried, Carry



def carry(root: Path, *, apply: bool) -> int:
    path = ExternalTranscript.path_in(root)
    if not path.exists():
        return 0
    with closing(CoordinationStore.writer(path)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            table = ExternalItemRow.declared_name
            fields = ExternalItemRow._fields()
            actual = tuple(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))
            expected = tuple(item.wire_name for item in fields)
            if actual == expected:
                for row in ExternalItemRow.stream(db):
                    pass
                db.rollback()
                return 0
            original = tuple(item.wire_name for item in fields if item.name != 'earlier')
            if actual != original:
                raise ValueError(f'Unexpected external item columns: {actual!r}')
            continuation, = (item for item in fields if item.name == 'earlier')
            selected = ', '.join(f'? AS "{item.wire_name}"' if item.name == 'earlier'
                                 else f'"{item.wire_name}"' for item in fields)
            count = sum(1 for row in ExternalItemRow.iterate(db.execute(
                f'SELECT {selected} FROM "{table}"', (continuation.encode(NoEarlierPage()),))))
            if not apply:
                db.rollback()
                return count
            saved = f'{table}_before_pages'
            db.execute(f'ALTER TABLE "{table}" RENAME TO "{saved}"')
            ExternalItemRow.create(db)
            columns = ExternalItemRow._column_list(expected)
            changed = db.execute(f'INSERT INTO "{table}" ({columns}) SELECT {selected} FROM "{saved}"',
                                 (continuation.encode(NoEarlierPage()),)).rowcount
            db.execute(f'DROP TABLE "{saved}"')
            db.commit() if apply else db.rollback()
            return changed
        except BaseException:
            db.rollback()
            raise


class ExternalPages(Carry):
    """External item rows gain the continuation to their earlier page."""

    after = 'carry_route_package'

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        changed = carry(root, apply=apply)
        rewritten = tuple(path.name for path in root.glob(f'{ExternalTranscript.path_in(root).name}*'))
        return Carried(changed, {}, rewritten if changed and apply else ())


if __name__ == '__main__':
    ExternalPages.main()
