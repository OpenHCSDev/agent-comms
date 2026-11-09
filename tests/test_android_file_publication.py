"""Native Android publication contracts without a Python or kernel hard-link path."""
import os
import stat

import pytest

from agent_comms.coordination.coordinator import StandaloneCoordination
from agent_comms.execution.child_process import (
    AndroidPlatform,
    Platform,
)
from agent_comms.execution.runtime_info import RuntimeInfoStore
from agent_comms.storage.store_files import ExclusiveAtomicFile, ExclusiveDirectoryAtomicFile


@pytest.fixture(autouse=True)
def android_without_hardlinks(monkeypatch):
    monkeypatch.setattr(Platform, 'current', classmethod(lambda cls: AndroidPlatform()))

    def refused(*args, **kwargs):
        raise AssertionError('Android does not permit hard links')

    monkeypatch.setattr(os, 'link', refused)


@pytest.mark.parametrize('anchored', [False, True])
def test_android_publishes_without_replacing_a_competing_file(tmp_path, anchored):
    target = tmp_path / 'published'
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        writer = (ExclusiveDirectoryAtomicFile(target, directory=descriptor) if anchored
                  else ExclusiveAtomicFile(target))
        writer.write_text('winner')
        inode = target.stat().st_ino
        with pytest.raises(FileExistsError):
            writer.write_text('loser')
        assert target.read_text() == 'winner'
        assert target.stat().st_ino == inode
        assert list(tmp_path.iterdir()) == [target]
    finally:
        os.close(descriptor)


def test_android_initializes_and_reopens_the_same_coordination_inode(tmp_path):
    path = tmp_path / 'coordination.sqlite3'
    with StandaloneCoordination(str(path)):
        inode = path.stat().st_ino
        with StandaloneCoordination(str(path)):
            assert path.stat().st_ino == inode
    assert not list(tmp_path.glob('.coord-init-*'))


def test_android_snapshot_retains_bytes_mode_and_exclusive_destination(tmp_path):
    source, snapshot = tmp_path / 'source', tmp_path / 'snapshot'
    source.write_bytes(b'original')
    source.chmod(0o640)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        platform = Platform.current()
        platform.snapshot_file(source.name, snapshot.name,
                               src_dir_fd=descriptor, dst_dir_fd=descriptor)
        source.write_bytes(b'next')
        assert snapshot.read_bytes() == b'original'
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o640
        with pytest.raises(FileExistsError):
            platform.snapshot_file(source, snapshot)
        assert snapshot.read_bytes() == b'original'
        assert not platform.share_file(source, tmp_path / 'not-shared')
        assert not (tmp_path / 'not-shared').exists()
    finally:
        os.close(descriptor)


def test_android_failed_document_publication_restores_bytes_and_mode(tmp_path, monkeypatch):
    target = tmp_path / 'runtime.json'
    target.write_text('{"before": 1}\n')
    target.chmod(0o640)
    replace = os.replace
    failed = False

    def fail_first(source, destination):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError('publication failed')
        return replace(source, destination)

    monkeypatch.setattr(os, 'replace', fail_first)
    with pytest.raises(OSError, match='publication failed'):
        RuntimeInfoStore(target)._write_unlocked('{"after": 2}\n')
    assert target.read_text() == '{"before": 1}\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert list(tmp_path.iterdir()) == [target]


def test_android_failed_snapshot_discards_the_partial_copy(tmp_path, monkeypatch):
    source, snapshot = tmp_path / 'source', tmp_path / 'snapshot'
    source.write_text('original')

    def failed(descriptor):
        raise OSError('snapshot sync failed')

    monkeypatch.setattr(os, 'fsync', failed)
    with pytest.raises(OSError, match='snapshot sync failed'):
        Platform.current().snapshot_file(source, snapshot)
    assert source.read_text() == 'original'
    assert not snapshot.exists()
