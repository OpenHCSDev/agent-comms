"""Registered storage is the single owner; ordinary edits do not walk or save it."""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.native_tool_call import SelectedToolDenied
from agent_comms.storage_rules import (
    AllocateStorageEffect, RemoveCheckoutStorageEffect, StorageRoots, StorageRule,
    WriteStorageEffect, shell_storage_effects,
)
from agent_comms.threads import Thread


def test_storage_paths_round_trip_inside_collections(tmp_path):
    roots = StorageRoots((tmp_path / 'home', tmp_path / 'extra'), tmp_path / 'scratch')
    encoded = FieldCodec.encode(roots)
    assert encoded['checkouts'] == [str(p) for p in roots.checkouts]
    assert FieldCodec.decode(StorageRoots, encoded) == roots


def test_registry_rule_supplies_new_thread_roots(tmp_path):
    import json
    (tmp_path / 'storage.json').write_text(json.dumps({
        'scratch_base': str(tmp_path / 'scratch'), 'floor_gib': 47,
    }))
    comms = Comms(tmp_path)
    rule = comms.registry.storage_rule
    thread = Thread.create('worker', frozenset(), str(tmp_path / 'home'), storage_rule=rule)
    assert thread.storage_roots.scratch == tmp_path / 'scratch' / 'worker'
    assert rule.floor_gib == 47
    assert comms.registry.declare(thread).storage_roots == thread.storage_roots


def test_ordinary_write_neither_walks_usage_nor_opens_registry_edit(tmp_path, monkeypatch):
    comms = Comms(tmp_path)
    thread = Thread.create('worker', frozenset(), str(tmp_path), storage_rule=comms.registry.storage_rule)
    comms.registry.declare(thread)
    def forbidden(*args, **kwargs):
        raise AssertionError('ordinary write performed allocation/registry work')
    monkeypatch.setattr(StorageRoots, 'usage', forbidden)
    monkeypatch.setattr(type(comms.registry.store), 'editing', forbidden)
    comms.registry.decide_storage('worker', (WriteStorageEffect(thread.storage_roots.scratch / 'result'),))


@pytest.mark.parametrize('index', [1, 2])
def test_any_registered_extra_can_be_removed_including_after_shell_error(tmp_path, monkeypatch, index):
    from agent_comms.channel_coding_tools import CodingCall
    from agent_comms.native_tools import ReadTool
    comms = Comms(tmp_path)
    thread = Thread.create('worker', frozenset(), str(tmp_path / 'home'), storage_rule=comms.registry.storage_rule)
    roots = thread.storage_roots.adding(tmp_path / 'first').adding(tmp_path / 'second')
    comms.registry.declare(replace(thread, storage_roots=roots))
    removed = roots.checkouts[index]
    decision = comms.registry.decide_storage('worker', (RemoveCheckoutStorageEffect(removed),))
    assert comms.registry.require('worker').storage_roots == roots
    call = CodingCall('removed-then-error', ReadTool(path='unused'))
    call.storage_decisions = (decision,)
    monkeypatch.setattr('agent_comms.channel_coding_tools.record_selected_terminal', lambda *args: None)
    call.commit_terminal(True, tmp_path, 'input', 1)
    assert comms.registry.require('worker').storage_roots == roots.removing((removed,))


def test_allocation_floor_is_checked_before_usage_walk(tmp_path, monkeypatch):
    rule = StorageRule(volume=tmp_path)
    roots = rule.initial_roots('worker', str(tmp_path))
    monkeypatch.setattr(StorageRule, 'free_gib', lambda self: 0)
    monkeypatch.setattr(StorageRoots, 'usage', lambda self: pytest.fail('walked before rejecting allocation'))
    with pytest.raises(SelectedToolDenied, match='allocation requires'):
        AllocateStorageEffect().admit(rule, roots)


@pytest.mark.parametrize('command', ['git clone remote target', 'python -m venv env', 'uv sync'])
def test_forbidden_shell_operations_use_declared_rule(tmp_path, command):
    rule = StorageRule()
    roots = rule.initial_roots('worker', str(tmp_path))
    with pytest.raises(SelectedToolDenied):
        for effect in shell_storage_effects(command, tmp_path):
            effect.admit(rule, roots)
