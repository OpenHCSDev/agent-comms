"""File contents and directory names become durable through the same writer."""

import os
import stat

import pytest

from agent_comms.child_process import Platform, Win32Platform
from agent_comms.store_files import AtomicFile, ExclusiveAtomicFile


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory flush")
def test_text_and_binary_writes_sync_contents_then_the_directory(tmp_path, monkeypatch):
    target = tmp_path / "value"
    synced = []
    fsync = os.fsync

    def observed(descriptor):
        synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", observed)
    AtomicFile(target, mode=0o640).write_text("first α")
    assert target.read_text() == "first α"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    AtomicFile(target).write_bytes(b"second")
    assert target.read_bytes() == b"second"
    assert synced == [False, True, False, True]


def test_failed_stream_preparation_keeps_the_old_file(tmp_path):
    target = tmp_path / "value"
    target.write_bytes(b"original")
    with pytest.raises(ValueError, match="stream failed"):
        with AtomicFile(target).staged() as output:
            output.write(b"partial")
            raise ValueError("stream failed")
    assert target.read_bytes() == b"original"
    assert set(tmp_path.iterdir()) == {target}


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory flush")
def test_parent_sync_failure_is_reported_after_the_visible_replace(tmp_path, monkeypatch):
    target = tmp_path / "value"
    fsync = os.fsync

    def failed_directory(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory sync failed")
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", failed_directory)
    with pytest.raises(OSError, match="directory sync failed"):
        AtomicFile(target).write_bytes(b"visible but not attested durable")
    assert target.read_bytes() == b"visible but not attested durable"


def test_exclusive_publication_never_replaces_an_existing_destination(tmp_path):
    target = tmp_path / "value"
    ExclusiveAtomicFile(target).write_text("original")
    with pytest.raises(FileExistsError):
        ExclusiveAtomicFile(target).write_text("replacement")
    assert target.read_text() == "original"
    assert set(tmp_path.iterdir()) == {target}


def test_windows_publication_and_existing_file_sync_use_writable_handles(tmp_path, monkeypatch):
    # These file operations need no kernel32 process handles, so exercise the
    # actual Windows implementation on either host without initializing them.
    platform = object.__new__(Win32Platform)
    monkeypatch.setattr(Platform, "current", classmethod(lambda cls: platform))
    target = tmp_path / "value"
    AtomicFile(target).write_bytes(b"first")
    staged = tmp_path / "prepared"
    staged.write_bytes(b"second")
    AtomicFile(target).replace(staged)
    AtomicFile(target).sync()
    with platform.open_for_sync(target) as source:
        assert source.writable()
        assert source.read() == b"second"
    # A Windows directory flush does not open the directory, including a path
    # that does not exist; it must not ask os for POSIX-only flags.
    platform.fsync_directory(tmp_path / "absent")
