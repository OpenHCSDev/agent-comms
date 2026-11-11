"""Real registry/turn files and the public Comms phase entrypoint."""

import json
import os
from shutil import copytree

import pytest

from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.nominal.field_codec import TurnId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import RegistryFile


def test_copied_registry_resolves_turn_from_its_new_location(tmp_path):
    source = tmp_path / 'source'
    copied = tmp_path / 'copied'
    comms = wire(source)
    comms.registry.declare(NativeThread(
        'worker', frozenset(), Checkout(source),
        process_identity=ProcessIdentity.capture(os.getpid())))
    original = comms.agents.begin_turn(
        'worker', TurnId('0d8da7dc169a0c12f0a6f121b819ca38')
    ).turn_lease.record
    raw = comms.registry.store.stored()
    reference = raw['threads']['worker']['turn_record']
    assert reference == {}
    copytree(source, copied)

    moved = RegistryStore(RegistryFile.path_in(copied))
    entry = moved.read_entry('worker').entry.thread
    assert entry.turn_record.read().id == '0d8da7dc169a0c12f0a6f121b819ca38'
    assert entry.turn_record.path == copied / 'turns' / original.path.name
    assert moved.read().threads['worker'].turn_record.path == entry.turn_record.path
    assert moved.read().threads['worker'].turn_record.registry_path == RegistryFile.path_in(copied)

    forged = RegistryStore(RegistryFile.path_in(copied)).stored()
    forged['threads']['worker']['turn_record']['lookup'] = 'wrong-identity'
    RegistryStore(RegistryFile.path_in(copied)).rewrite_stored(lambda _: forged)
    with pytest.raises(RelationViolationError):
        RegistryStore(RegistryFile.path_in(copied)).read_entry('worker')
