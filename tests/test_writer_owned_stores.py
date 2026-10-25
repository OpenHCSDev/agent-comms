"""Install checks exercise the stored bytes through the declared row decoder."""

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import pytest

from agent_comms.channel_targets import ChannelTarget, CommandTarget
from agent_comms.errors import RelationViolationError
from agent_comms.locked_store import SQLiteFile
from agent_comms.typed_table import (
    Column, DurableTables, SharedFileTables, TableSchema, TypedTable,
    WithoutRowid, WriterOwnedFile, WriterOwnedStore,
)


@pytest.fixture
def writer_owned_store(tmp_path):
    class SavedTabTables(SharedFileTables, DurableTables):
        """The saved view's declaration, not a second schema in the test."""

    @dataclass(frozen=True)
    class SavedTab(SavedTabTables, TypedTable, declared_name="open_tabs"):
        target: Annotated[CommandTarget, Column(primary_key=True)]
        position: int

    stored = tmp_path / "state" / "saved.db"
    stored.parent.mkdir()

    class SavedViewStore(WriterOwnedStore):
        @classmethod
        def path(cls) -> Path:
            return stored

        @classmethod
        def table_schema(cls) -> type[TableSchema]:
            return SavedTabTables

    with closing(SQLiteFile.writer(stored)) as db, db:
        SavedTabTables.install_schema(db, WriterOwnedFile())
    try:
        yield SavedViewStore, SavedTab
    finally:
        stored.unlink(missing_ok=True)


def test_writer_owned_carry_refuses_an_undecodable_target_with_its_rowid(
    writer_owned_store, tmp_path,
):
    store, row = writer_owned_store
    with closing(SQLiteFile.writer(store.path())) as db, db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute('INSERT INTO open_tabs VALUES (?, ?)', ('{"name":"#comms"}', 0))
    original = store.path().read_bytes()
    with pytest.raises(RelationViolationError) as refused:
        store.practice_opening(tmp_path)
    text = str(refused.value)
    assert str(store.path()) in text
    assert "open_tabs rowid 1 cannot decode" in text
    assert "kind" in text
    assert store.path().read_bytes() == original


def test_writer_owned_carry_opens_valid_targets_without_writing_the_source(
    writer_owned_store, tmp_path,
):
    store, row = writer_owned_store
    with closing(SQLiteFile.writer(store.path())) as db, db:
        row(ChannelTarget("#comms"), 0).insert(db)
    original = store.path().read_bytes()
    store.practice_opening(tmp_path)
    assert store.path().read_bytes() == original


def test_writer_owned_carry_validates_without_rowid_tables(tmp_path):
    class KeyedTables(SharedFileTables, DurableTables):
        """A writer can own keyed tables as well as SQLite-numbered ones."""

    @dataclass(frozen=True)
    class SavedKey(KeyedTables, WithoutRowid, declared_name="saved_keys"):
        key: Annotated[str, Column(primary_key=True)]
        target: CommandTarget

    stored = tmp_path / "keyed.db"

    class KeyedStore(WriterOwnedStore):
        @classmethod
        def path(cls) -> Path:
            return stored

        @classmethod
        def table_schema(cls) -> type[TableSchema]:
            return KeyedTables

    with closing(SQLiteFile.writer(stored)) as db, db:
        KeyedTables.install_schema(db, WriterOwnedFile())
        SavedKey("first", ChannelTarget("#comms")).insert(db)
    directory = tmp_path / "copy"
    directory.mkdir()
    try:
        KeyedStore.practice_opening(directory)
    finally:
        stored.unlink(missing_ok=True)
