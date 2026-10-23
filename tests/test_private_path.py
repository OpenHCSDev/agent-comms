"""Private path roles against actual POSIX file, directory and socket nodes."""

import os
import socket
from pathlib import Path

import pytest

from agent_comms.child_process import Platform
from agent_comms.private_path import (
    FileIdentity,
    FileRevision,
    PrivateDirectoryRole,
    PrivateFileRole,
    PrivateSocketRole,
    TrustedAncestorRole,
)


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes and no-follow directory opens")
def test_platform_privacy_and_durability_follow_actual_nodes(tmp_path):
    platform = Platform.current()
    file = tmp_path / "data"
    file.write_bytes(b"durable bytes")
    directory = tmp_path / "private"
    directory.mkdir()
    for path, private_mode, public_mode in (
        (file, 0o600, 0o644), (directory, 0o700, 0o755),
    ):
        path.chmod(public_mode)
        assert not platform.is_private(path.stat(), private_mode)
        platform.make_private(path, private_mode)
        assert platform.is_private(path.stat(), private_mode)
    platform.make_private(tmp_path / "retired", 0o600)
    platform.sync_file(file)
    platform.sync_directory(directory)
    assert file.read_bytes() == b"durable bytes"
    redirected = tmp_path / "redirected"
    redirected.symlink_to(directory, target_is_directory=True)
    with pytest.raises(OSError):
        platform.sync_directory(redirected)


def test_private_roles_and_revisions_follow_original_native_nodes(tmp_path: Path):
    file = tmp_path / "source.jsonl"
    file.write_bytes(b"header\n")
    file.chmod(0o600)
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    endpoint = directory / "gateway.sock"
    with socket.socket(socket.AF_UNIX) as peer:
        # The persistent worktree/test path exceeds Linux's sun_path limit.
        # Bind through its open directory; inspect the actual named node below.
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            peer.bind(f"/proc/self/fd/{directory_fd}/gateway.sock")
        finally:
            os.close(directory_fd)
        endpoint.chmod(0o600)
        for role, path in (
            (PrivateFileRole, file),
            (PrivateDirectoryRole, directory),
            (PrivateSocketRole, endpoint),
            (TrustedAncestorRole, directory),
        ):
            role.require(path.lstat())
            path.chmod(path.stat().st_mode | 0o022)
            with pytest.raises(ValueError):
                role.require(path.lstat())
            path.chmod(0o600 if path != directory else 0o700)
        with pytest.raises(ValueError):
            PrivateFileRole.require(endpoint.lstat())
        redirected = directory / "source"
        redirected.symlink_to(file)
        with pytest.raises(ValueError):
            PrivateFileRole.require(redirected.lstat())

    before = FileRevision.from_stat(file.stat())
    with file.open("ab") as stream:
        stream.write(b"message\n")
        stream.flush()
        os.fsync(stream.fileno())
    after = FileRevision.from_stat(file.stat())
    assert before.identity == after.identity == FileIdentity.from_stat(file.stat())
    assert after != before and after.size == before.size + len(b"message\n")
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(file.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(file)
    assert FileIdentity.from_stat(file.stat()) != before.identity
