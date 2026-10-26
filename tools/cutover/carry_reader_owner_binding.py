"""Carry bridge reader addresses onto their registered incarnations, preserving deliveries."""

import sqlite3
from contextlib import closing
from typing import ClassVar

from agent_comms.cli_bridges.shared import Binding, BridgeDeliveryStore
from agent_comms.errors import RelationViolationError
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.typed_table import WriterOwnedFile, WriterOwnedResource


class ReaderOwnerBinding(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = ()
    writer_owned = (BridgeDeliveryStore,)

    def carry(self, *, apply: bool) -> Carried:
        pending: list[WriterOwnedResource] = []
        for resource in BridgeDeliveryStore.resources(self.files):
            path = self.files.location(resource)
            self.require_confined(path)
            if not path.is_file():
                continue
            with closing(BridgeDeliveryStore.read_snapshot(path)) as source:
                if 'bus' not in Binding.stored_columns(source):
                    continue
                try:
                    with closing(sqlite3.connect(':memory:')) as copied:
                        source.backup(copied)
                        BridgeDeliveryStore.table_schema().install_schema(copied, WriterOwnedFile())
                        BridgeDeliveryStore.table_schema().require_readable(copied)
                except RelationViolationError as error:
                    raise RelationViolationError(f'{path}: {error}') from error
            pending.append(resource)
        if apply:
            for resource in pending:
                path = self.files.location(resource)
                with closing(BridgeDeliveryStore.writer(path)) as db:
                    BridgeDeliveryStore.table_schema().install_schema(db, WriterOwnedFile())
                    BridgeDeliveryStore.table_schema().require_readable(db)
        return Carried(len(pending), rewritten=tuple(resource.path for resource in pending)
                       if apply else ())
