"""Install checks exercise the stored bytes through the declared row decoder."""

from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, ClassVar

import pytest

from agent_comms.locked_store import LiveFilesRequest, RetainedCopyRequest
from agent_comms.channel_targets import ChannelTarget, CommandTarget
from agent_comms.cli import build_parser
from agent_comms.cli_commands import PlannedReconcileCliCommand
from agent_comms.field_codec import FieldCodec
from agent_comms.errors import RelationViolationError
from agent_comms.installer.backup import StoreBackup
from agent_comms.installer.practice import PracticeCopy
from agent_comms.installer.probes.carry import Carried, Carry, WriterOwnedOpeningProbe
from agent_comms.locked_store import RootFile, SQLiteFile
from agent_comms.typed_table import (
    Column, DurableTables, SharedFileTables, TableSchema, TypedTable,
    WithoutRowid, WriterOwnedFile, WriterOwnedStore,
)


BARE_TARGET = '{"name":"#comms"}'


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
        db.execute('INSERT INTO open_tabs VALUES (?, ?)', (BARE_TARGET, 0))
    original = store.path().read_bytes()
    with pytest.raises(RelationViolationError) as refused:
        store.practice_opening(LiveFilesRequest(tmp_path), tmp_path)
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
    store.practice_opening(LiveFilesRequest(tmp_path), tmp_path)
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
        KeyedStore.practice_opening(LiveFilesRequest(directory), directory)
    finally:
        stored.unlink(missing_ok=True)


@pytest.fixture
def writer_owned_step(writer_owned_store):
    store, row = writer_owned_store

    class SavedViewCarry(Carry):
        opened_files: ClassVar[tuple[type[RootFile], ...]] = ()
        writer_owned = (store,)

        def carry(self, *, apply: bool) -> Carried:
            path = self.writer_path(store)
            self.require_confined(path)
            with closing(store.read_snapshot(path)) as db:
                (count,) = db.execute(
                    "SELECT count(*) FROM open_tabs WHERE target = ?", (BARE_TARGET,),
                ).fetchone()
            if apply and count:
                (column,) = row._fields_named(("target",))
                with closing(store.writer(path)) as db, db:
                    self.rewrite_stored(db, row, column, [
                        (column.encode(ChannelTarget("#comms")), BARE_TARGET),
                    ])
            return Carried(count, rewritten=(store.retained_filename(),)
                           if apply and count else ())

    return SavedViewCarry


def test_writer_owned_carry_copies_migrates_and_restores_original_bytes(
    writer_owned_store, writer_owned_step, tmp_path,
):
    store, row = writer_owned_store
    with closing(SQLiteFile.writer(store.path())) as db, db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("INSERT INTO open_tabs VALUES (?, ?)", (BARE_TARGET, 0))
    original = store.path().read_bytes()
    root = tmp_path / "bus"
    root.mkdir()
    step = writer_owned_step(LiveFilesRequest(root), tmp_path / "spill")
    assert step.input_files() == (store.path(),)
    assert step.carry(apply=False).changed == 1
    name = store.retained_filename()
    backup = StoreBackup.take(root, tmp_path / "route", step.input_files(), tmp_path / "backup")
    assert backup.selected_files() == (name,)
    assert name == Path("writer-owned") / store.declared_name
    assert Carried(1, rewritten=(store.path(),)).for_step(step).rewritten == (name,)
    assert Carried(1, rewritten=(name,)).for_step(step).rewritten == (name,)

    copy = PracticeCopy(root, tmp_path / "practice")
    copy.include(step.input_files())
    copied_step = replace(step, files=RetainedCopyRequest(copy.path))
    copied_path = copied_step.writer_path(store)
    assert Carried(1, rewritten=(copied_path,)).for_step(copied_step).rewritten == (name,)
    with pytest.raises(RelationViolationError):
        copied_step.require_confined(store.path())
    assert copied_step.carry(apply=True).rewritten == (name,)
    assert WriterOwnedOpeningProbe(RetainedCopyRequest(copy.path), (store,)).run() == (copied_path,)
    with closing(store.read_snapshot(copied_path)) as db:
        assert row.select(db)[0].target == ChannelTarget("#comms")
    assert store.path().read_bytes() == original

    assert step.carry(apply=True).changed == 1
    assert not backup.stores_original()
    backup.restore()
    assert backup.stores_original()
    assert store.path().read_bytes() == original


def test_writer_owned_carry_refuses_an_undeclared_external_file(
    writer_owned_step, tmp_path,
):
    root = tmp_path / "bus"
    root.mkdir()
    step = writer_owned_step(LiveFilesRequest(root), tmp_path / "spill")
    outside = tmp_path / "unrelated.db"
    outside.touch()
    with pytest.raises(RelationViolationError):
        step.require_confined(outside)
    with pytest.raises(RelationViolationError):
        StoreBackup.take(root, tmp_path / "route", (outside,), tmp_path / "backup")
    alias = root / "alias"
    alias.symlink_to(outside)
    with pytest.raises(RelationViolationError):
        step.require_confined(alias)


def test_writer_owned_carry_preserves_its_declaration_across_cli_and_worker_boundaries(
    writer_owned_store, tmp_path,
):
    store, row = writer_owned_store
    parser = build_parser()
    command = PlannedReconcileCliCommand.from_arguments(parser.parse_args([
        "planned-reconcile", "--carrying", store.declared_name,
    ]))
    assert command.carrying == (store,)
    empty = PlannedReconcileCliCommand.from_arguments(parser.parse_args([
        "planned-reconcile", "--carrying",
    ]))
    assert empty.carrying == ()
    probe = WriterOwnedOpeningProbe(LiveFilesRequest(tmp_path), (store,))
    decoded = FieldCodec.decode(WriterOwnedOpeningProbe, FieldCodec.encode(probe))
    assert decoded == probe
    assert decoded.run() == (store.path(),)
    copied_root = tmp_path / "copied"
    copied_probe = WriterOwnedOpeningProbe(RetainedCopyRequest(copied_root), (store,))
    copied_decoded = FieldCodec.decode(WriterOwnedOpeningProbe, FieldCodec.encode(copied_probe))
    assert copied_decoded.stores == (store,)
    assert copied_decoded.files.root == copied_root
    assert copied_decoded == copied_probe


def test_writer_owned_backup_preserves_primary_and_committed_wal_bytes(
    writer_owned_store, writer_owned_step, tmp_path,
):
    store, row = writer_owned_store
    root = tmp_path / "bus"
    root.mkdir()
    step = writer_owned_step(LiveFilesRequest(root), tmp_path / "spill")
    wal = SQLiteFile.wal_path(store.path())
    with closing(SQLiteFile.writer(store.path())) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("INSERT INTO open_tabs VALUES (?, ?)", (BARE_TARGET, 0))
        db.commit()
        original = store.path().read_bytes()
        committed_wal = wal.read_bytes()
        backup = StoreBackup.take(root, tmp_path / "route", step.input_files(), tmp_path / "backup")
        assert store.path().read_bytes() == original
        assert wal.read_bytes() == committed_wal
    # The writer is closed before any replacement: a bus hold alone does not stop it.
    assert step.carry(apply=True).changed == 1
    backup.restore()
    assert backup.stores_original()
    assert store.path().read_bytes() == original
    assert wal.read_bytes() == committed_wal
    with closing(SQLiteFile.read_snapshot(store.path())) as db:
        assert db.execute("SELECT target FROM open_tabs").fetchall()[0][0] == BARE_TARGET
