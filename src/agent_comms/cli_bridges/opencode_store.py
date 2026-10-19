"""OpenCode sessions as OpenCode stores them: an export file, or its own database.

Both hold the messages the running server returns, so the bridge's message decoder reads them.
"""
from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass

from pydantic import Field

from agent_comms.cli_protocol import WireModel
from agent_comms.coordination_database import CoordinationStore
from agent_comms.runtime_updates import RuntimeUpdate
from agent_comms.typed_table import SQLiteSchemaObject, TypedRow

from .history import OfflineHistory, OpenCodeHistory
from .opencode_parts import SavedMessage
from .opencode_protocol import Revert, Session


class OpenCodeStore(OpenCodeHistory, OfflineHistory):
    @abstractmethod
    def load(self) -> tuple[Session, Iterator[SavedMessage]]: ...

    def read(self) -> Iterator[RuntimeUpdate]:
        return self.saved(*self.load())


class ExportFile(WireModel):
    info: Session
    messages: list[SavedMessage] = Field(default_factory=list)


class OpenCodeExport(OpenCodeStore):
    """What `opencode export` writes."""

    suffixes = frozenset({'.json'})

    def load(self) -> tuple[Session, Iterator[SavedMessage]]:
        export = ExportFile.model_validate_json(self.source.read_bytes())
        if self.session_id not in (None, export.info.id):
            raise ValueError('The export belongs to a different OpenCode session.')
        return export.info, iter(export.messages)


@dataclass(frozen=True)
class SessionRow(TypedRow):
    id: str
    directory: str
    title: str
    revert: str | None


@dataclass(frozen=True)
class DataRow(TypedRow):
    id: str
    data: str


@dataclass(frozen=True)
class LinkedHistory(TypedRow):
    present: bool


class OpenCodeDatabase(OpenCodeStore):
    """OpenCode's own `opencode.db`, read from a snapshot so the running OpenCode is not disturbed.
    Its rows keep ids in columns that the server puts inside each message and part."""

    suffixes = frozenset({'.db', '.sqlite', '.sqlite3'})

    def load(self) -> tuple[Session, Iterator[SavedMessage]]:
        if not self.session_id:
            raise ValueError('OpenCode database imports require --session-id.')
        with closing(CoordinationStore.read_snapshot(self.source)) as db:
            sessions = SessionRow.read(db.execute(
                'SELECT id,directory,title,revert FROM session WHERE id=?', (self.session_id,)))
            if not sessions:
                raise ValueError(f'OpenCode session {self.session_id!r} was not found.')
            row, = sessions
            tables = {table.name for table in SQLiteSchemaObject.read(
                db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))}
            if 'session_message' in tables and LinkedHistory.read(db.execute(
                    'SELECT EXISTS(SELECT 1 FROM session_message WHERE session_id=?) AS present',
                    (self.session_id,))) == [LinkedHistory(True)]:
                raise ValueError('This OpenCode database uses linked history; import an opencode export JSON instead.')
            session = Session(id=row.id, directory=row.directory, title=row.title,
                              revert=Revert.model_validate_json(row.revert or '{}'))
            return session, iter(tuple(self.messages(db)))

    def messages(self, db) -> Iterator[SavedMessage]:
        for message in DataRow.read(db.execute(
                'SELECT id,data FROM message WHERE session_id=? ORDER BY time_created,id', (self.session_id,))):
            parts = DataRow.read(db.execute(
                'SELECT id,data FROM part WHERE message_id=? ORDER BY time_created,id', (message.id,)))
            yield SavedMessage.model_validate({
                'info': {**json.loads(message.data), 'id': message.id, 'sessionID': self.session_id},
                'parts': [{**json.loads(part.data), 'id': part.id, 'sessionID': self.session_id,
                           'messageID': message.id} for part in parts],
            })
