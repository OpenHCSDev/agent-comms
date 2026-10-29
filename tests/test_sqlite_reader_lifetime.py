"""Physical reader ownership; authored for root qualification, not author-run."""

import gc
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from agent_comms.locked_store import FileStore, SQLiteReader, SQLiteStorage


class ReaderFile(FileStore, SQLiteReader):
    """Compose the real physical-path and read-transaction owners without overrides."""


def database(path, value):
    with closing(SQLiteStorage.writer(path)) as db:
        db.execute("CREATE TABLE value (number INTEGER NOT NULL)")
        db.execute("INSERT INTO value VALUES (?)", (value,))
        db.commit()
    path.chmod(0o600)
    return ReaderFile(path)


def value(db):
    return db.execute("SELECT number FROM value").fetchone()[0]


@pytest.mark.parametrize("change", ("replace", "unlink"))
def test_nested_cut_survives_physical_path_change(tmp_path, change):
    path = tmp_path / "read.sqlite3"
    store = database(path, 1)
    replacement = database(tmp_path / "replacement.sqlite3", 2)
    with store.reader_snapshot() as outer:
        assert value(outer) == 1
        if change == "replace":
            replacement.path.replace(path)
        else:
            path.unlink()
        with store.reader_snapshot() as nested:
            assert nested is outer and value(nested) == 1
        assert outer.in_transaction and value(outer) == 1
    assert not outer.in_transaction
    if change == "replace":
        with store.reader_snapshot() as later:
            assert later is not outer and value(later) == 2
    else:
        with pytest.raises(FileNotFoundError):
            with store.reader_snapshot():
                pytest.fail("A new outer read cannot borrow the retired cut")


def test_same_thread_reuses_handle_and_only_outer_scope_ends_the_cut(tmp_path):
    store = database(tmp_path / "read.sqlite3", 1)
    with store.reader_snapshot() as outer:
        with store.reader_snapshot() as nested:
            assert nested is outer and value(nested) == 1
        assert outer.in_transaction
        with pytest.raises(sqlite3.OperationalError):
            outer.execute("INSERT INTO value VALUES (2)")
    with store.reader_snapshot() as later:
        assert later is outer and value(later) == 1


def test_threads_and_store_instances_do_not_share_physical_handles(tmp_path):
    store = database(tmp_path / "read.sqlite3", 1)
    other_store = ReaderFile(store.path)
    with store.reader_snapshot() as main, other_store.reader_snapshot() as other:
        assert main is not other
        with ThreadPoolExecutor(max_workers=1) as worker:
            def observe():
                with store.reader_snapshot() as foreign:
                    assert foreign is not main and foreign is not other
                    return value(foreign)
            assert worker.submit(observe).result(timeout=3) == 1
        assert value(main) == value(other) == 1


def test_store_lifetime_retires_the_physical_resource(tmp_path):
    store = database(tmp_path / "read.sqlite3", 1)
    with store.reader_snapshot() as db:
        assert value(db) == 1
    del store
    gc.collect()
    with pytest.raises(sqlite3.ProgrammingError):
        value(db)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Requires actual fork identity")
def test_child_does_not_borrow_parents_active_cut(tmp_path):
    store = database(tmp_path / "read.sqlite3", 1)
    with store.reader_snapshot() as parent:
        assert value(parent) == 1
        child = os.fork()
        if child == 0:
            try:
                with store.reader_snapshot() as own:
                    assert own is not parent and value(own) == 1
                os._exit(0)
            except BaseException:
                os._exit(1)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert parent.in_transaction and value(parent) == 1
