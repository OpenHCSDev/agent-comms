"""A registry transition writes once; the live decoder keeps one goal shape."""
import json
from unittest.mock import patch

import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import Goal, ReportedTurn
from agent_comms.registration import Registration
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryGoalConversion, RegistryStore


def old_registry(path):
    registry = Registration(path)
    registry.register(registry.new_thread('empty', frozenset(), str(path.parent)))
    registry.register(registry.new_thread(
        'present', frozenset(), str(path.parent),
        goal=Goal('Keep this goal', 'saved', 0, reported_turn=ReportedTurn('turn'))))
    raw = json.loads(path.read_text())
    del raw['version']
    raw['threads']['empty']['goal'] = {}
    path.write_text(json.dumps(raw))
    return raw


@pytest.mark.parametrize('read', [RegistryStore.read, lambda store: store.read_entry('empty')])
def test_first_open_converts_before_decoding_and_second_open_writes_nothing(tmp_path, read):
    path = tmp_path / 'registry.json'
    old = old_registry(path)
    with pytest.raises(RelationViolationError, match='NoGoal'):
        RegistryDocument.from_wire(old)
    with patch.object(RegistryGoalConversion, 'converted', wraps=RegistryGoalConversion.converted) as convert:
        read(RegistryStore(path))
        after = json.loads(path.read_text())
        assert after == {**old, 'version': RegistryGoalConversion.target_version, 'threads': {
            **old['threads'], 'empty': {**old['threads']['empty'], 'goal': None}}}
        assert RegistryStore(path).read().threads['present'].goal.reported_turn == ReportedTurn('turn')
        first = path.stat()
        read(RegistryStore(path))
        second = path.stat()
        assert (first.st_ino, first.st_mtime_ns, first.st_size) == (
            second.st_ino, second.st_mtime_ns, second.st_size)
        assert convert.call_count == 1


def test_a_current_document_does_not_reinterpret_a_bad_goal(tmp_path):
    path = tmp_path / 'registry.json'
    old = old_registry(path)
    old['version'] = RegistryGoalConversion.target_version
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    with pytest.raises(RelationViolationError, match='NoGoal'):
        RegistryStore(path).read()
    assert path.read_bytes() == before


@pytest.mark.parametrize('goal', [{'text': 'must not be cleared'}, {'kind': 'unknown'}])
def test_unrecognized_old_goals_are_not_discarded_or_partly_published(tmp_path, goal):
    path = tmp_path / 'registry.json'
    old = old_registry(path)
    old['threads']['present']['goal'] = goal
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    with pytest.raises(RelationViolationError):
        RegistryStore(path).read()
    assert path.read_bytes() == before


def test_future_document_version_is_refused_without_writing(tmp_path):
    path = tmp_path / 'registry.json'
    raw = FieldCodec.encode(RegistryDocument())
    raw['version'] = RegistryGoalConversion.target_version + 1
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    with pytest.raises(RelationViolationError, match='Unsupported registry version'):
        RegistryStore(path).read()
    assert path.read_bytes() == before
