"""Android's actual app-data ancestry through the registry and path owners."""
import os
import stat
import sys
from pathlib import Path

import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.nominal.field_codec import RootId
from agent_comms.registry.private_registry_guard import PrivateRegistryGuard
from agent_comms.storage.locked_store import RegistryFile
from agent_comms.storage.private_path import TrustedAncestorRole


def directory(mode, uid, gid):
    return os.stat_result((stat.S_IFDIR | mode, 1, 1, 1, uid, gid, 0, 0, 0, 0))


@pytest.fixture
def android(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'android')
    monkeypatch.setattr(os, 'geteuid', lambda: 10257)
    monkeypatch.setattr(os, 'getegid', lambda: 10257)


def test_android_registry_accepts_the_actual_termux_app_data_ancestors(android, monkeypatch):
    root = Path('/data/data/com.termux/files/home/wire')
    observed = {
        Path('/'): directory(0o755, 0, 0),
        Path('/data'): directory(0o771, 1000, 1000),
        Path('/data/data'): directory(0o771, 1000, 1000),
        Path('/data/data/com.termux'): directory(0o700, 10257, 10257),
        Path('/data/data/com.termux/files'): directory(0o771, 10257, 10257),
        Path('/data/data/com.termux/files/home'): directory(0o700, 10257, 10257),
        root: directory(0o700, 10257, 10257),
    }
    monkeypatch.setattr(Path, 'lstat', lambda path: observed[path])
    guard = PrivateRegistryGuard(RegistryFile.path_in(root), RootId('00' * 16))
    guard._trusted_root()
    TrustedAncestorRole.require_path(root, ValueError('Untrusted Android ancestor'))


@pytest.mark.parametrize('observed', [
    directory(0o771, 10258, 10258),  # Another app, not this process or the system.
    directory(0o771, 10257, 10258),  # Another app can write through the group.
    directory(0o777, 1000, 1000),   # System ownership does not excuse world writes.
    os.stat_result((stat.S_IFLNK | 0o700, 1, 1, 1, 10257, 10257, 0, 0, 0, 0)),
])
def test_android_ancestors_still_reject_foreign_writers_and_links(android, observed):
    with pytest.raises(ValueError, match='Untrusted Android ancestor'):
        TrustedAncestorRole.require(observed, ValueError('Untrusted Android ancestor'))


def test_android_registry_root_still_requires_owner_only_mode(android, monkeypatch):
    monkeypatch.setattr(Path, 'lstat', lambda path: directory(0o755, 10257, 10257))
    guard = PrivateRegistryGuard(Path('/wire/registry.json'), RootId('00' * 16))
    with pytest.raises(RelationViolationError, match='root must be owner-only'):
        guard._trusted_root()


def test_linux_does_not_inherit_android_system_or_app_group_authority(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(os, 'geteuid', lambda: 10257)
    for observed in (directory(0o755, 1000, 1000), directory(0o771, 10257, 10257)):
        with pytest.raises(ValueError):
            TrustedAncestorRole.require(observed, ValueError('Untrusted Linux ancestor'))
