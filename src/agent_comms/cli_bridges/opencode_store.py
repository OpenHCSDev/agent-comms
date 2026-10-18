"""OpenCode sessions as OpenCode stores them: an export file, or its own database.

Both hold the messages the running server returns, so the bridge's message decoder reads them.
"""
from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

from pydantic import Field

from agent_comms.cli_protocol import WireModel
from agent_comms.coordination_database import CoordinationStore
from agent_comms.harness_install import OpenCodeHarness
from agent_comms.typed_table import SQLiteSchemaObject, TypedRow

from .history import OfflineHistory
from .opencode_parts import SavedMessage
from .shared import SessionFacts, TranscriptUpdate


class Revert(WireModel):
    """The message a user reverted to; it and everything after it were undone."""
    messageID: str


class StoredSession(WireModel):
    id: str
    directory: str
    title: str | None = None
    revert: Revert | None = None


class OpenCodeStore(OfflineHistory):
    harness = OpenCodeHarness

    def __init__(self, source: Path, session_id: str | None) -> None:
        self.source = source
        self.session_id = session_id

    @classmethod
    def opened(cls, source: Path, session_id: str | None) -> Self:
        return cls(source, session_id)

    @abstractmethod
    def load(self) -> tuple[StoredSession, Iterator[SavedMessage]]: ...

    def read(self) -> Iterator[TranscriptUpdate]:
        session, messages = self.load()
        yield SessionFacts(source_id=session.id, project=session.directory, title=session.title)
        undone = session.revert.messageID if session.revert is not None else None
        for saved in messages:
            if saved.info.id == undone:
                return
            yield from saved.publications()


class ExportFile(WireModel):
    info: StoredSession
    messages: list[SavedMessage] = Field(default_factory=list)


class OpenCodeExport(OpenCodeStore):
    """What `opencode export` writes."""

    @classmethod
    def reads(cls, source: Path) -> bool:
        return source.suffix == '.json'

    def load(self) -> tuple[StoredSession, Iterator[SavedMessage]]:
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

    suffixes: ClassVar[frozenset[str]] = frozenset({'.db', '.sqlite', '.sqlite3'})

    @classmethod
    def reads(cls, source: Path) -> bool:
        return source.suffix in cls.suffixes

    def load(self) -> tuple[StoredSession, Iterator[SavedMessage]]:
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
            session = StoredSession(id=row.id, directory=row.directory, title=row.title,
                                    revert=Revert.model_validate_json(row.revert) if row.revert else None)
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
