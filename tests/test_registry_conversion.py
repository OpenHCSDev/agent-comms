"""A reader refuses old files; only the explicit installer operation writes them."""
import json

import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import MalformedRecord
from agent_comms.registration import Registration
from agent_comms.registry_store import RegistryNeedsConversion


def old_registry(tmp_path):
    registration = Registration(tmp_path / "registry.json")
    registration.register(registration.new_thread("worker", frozenset(), str(tmp_path)))
    store = registration.store
    raw = json.loads(store.path.read_text())
    raw.pop("version")
    raw["threads"]["worker"]["goal"] = {}
    store.path.write_text(json.dumps(raw))
    return store, raw


def test_reads_and_lock_acquisition_never_convert(tmp_path):
    store, raw = old_registry(tmp_path)
    before = store.path.read_bytes()
    with store.locked():
        assert store.path.read_bytes() == before
    for read in (store.read, lambda: store.read_entry("worker")):
        with pytest.raises(RegistryNeedsConversion, match=r"RegistryStore.convert_format\(\)"):
            read()
        assert store.path.read_bytes() == before
    assert store.convert_format(apply=False)
    assert store.path.read_bytes() == before
    assert store.convert_format()
    after = json.loads(store.path.read_text())
    expected = {**raw, "version": store.empty().version}
    expected["threads"]["worker"]["goal"] = None
    assert after == expected
    assert store.read().entry("worker").thread.goal.to_wire() is None
    assert store.read_entry("worker").entry.thread.name == "worker"
    converted = store.path.read_bytes()
    assert not store.convert_format()
    assert store.path.read_bytes() == converted


@pytest.mark.parametrize("change", ["unknown-version", "invalid-goal"])
def test_conversion_refuses_before_publication(tmp_path, change):
    store, raw = old_registry(tmp_path)
    if change == "unknown-version":
        raw["version"] = 987
    else:
        raw["threads"]["worker"]["goal"] = {"unrecognized": True}
    store.path.write_text(json.dumps(raw))
    before = store.path.read_bytes()
    with pytest.raises(RelationViolationError):
        store.convert_format()
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("raw", [None, [], True])
def test_nonobject_registry_is_refused_by_the_codec(tmp_path, raw):
    store, _ = old_registry(tmp_path)
    store.path.write_text(json.dumps(raw))
    before = store.path.read_bytes()
    for operation in (store.read, store.convert_format):
        with pytest.raises(MalformedRecord):
            operation()
        assert store.path.read_bytes() == before


def test_conversion_keeps_the_private_guard_authoritative(tmp_path):
    from agent_comms.comms import Comms

    store, raw = old_registry(tmp_path)
    path = store.path
    store.convert_format()
    Comms(tmp_path).bus.publisher.initialize_private_protocol()
    # Recreate old bytes through the existing private writer, so the guard
    # commits those bytes before the installer is asked to convert them.
    with store.locked():
        store._write_unlocked(json.dumps(raw, indent=2))
    assert store.convert_format()
    assert store.read().threads['worker'].goal.to_wire() is None
    path.write_bytes(path.read_bytes() + b' ')
    before = path.read_bytes()
    with pytest.raises(RelationViolationError):
        store.convert_format()
    assert path.read_bytes() == before


def test_current_version_still_requires_the_whole_valid_document(tmp_path):
    store, raw = old_registry(tmp_path)
    raw["version"] = store.empty().version
    store.path.write_text(json.dumps(raw))
    before = store.path.read_bytes()
    with pytest.raises(RelationViolationError):
        store.convert_format()
    assert store.path.read_bytes() == before
