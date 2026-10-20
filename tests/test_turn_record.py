"""Real registry/turn files and the public Comms phase entrypoint."""

import json
import os

import pytest
from dataclasses import replace
from shutil import copytree

from agent_comms.errors import RelationViolationError
from agent_comms.registry_store import RegistryStore
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.turn_phase import ModelWaitPhase, ToolRunningPhase
from agent_comms.turn_lease import NoTurnRecord, StoredTurnRecord, StoredTurnReference


def test_copied_registry_resolves_turn_from_its_new_location(tmp_path):
    source = tmp_path / 'source'
    copied = tmp_path / 'copied'
    comms = wire(source)
    comms.registry.declare(comms.registry.new_thread(
        'worker', frozenset(), str(source),
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    original = comms.agents.begin_turn('worker', 'preserved').turn_lease.record
    raw = json.loads((source / 'registry.json').read_text())
    reference = raw['threads']['worker']['turn_record']
    assert reference == {}
    copytree(source, copied)

    moved = RegistryStore(copied / 'registry.json')
    entry = moved.read_entry('worker').entry.thread
    assert entry.turn_record.read().id == 'preserved'
    assert entry.turn_record.path == copied / 'turns' / original.path.name
    assert moved.read().threads['worker'].turn_record.path == entry.turn_record.path
    assert moved.read().threads['worker'].turn_record.registry_path == copied / 'registry.json'

    forged = json.loads((copied / 'registry.json').read_text())
    forged['threads']['worker']['turn_record']['lookup'] = 'wrong-identity'
    with pytest.raises(RelationViolationError, match='Invalid registry document'):
        moved._decode(forged)


