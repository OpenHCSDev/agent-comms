"""A saved transcript is the bus's own or an external harness file, and a copy reads its own."""

import json
import shutil
from pathlib import Path

import pytest

from agent_comms.bus import Bus
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.historical_views import HistoryArchive
from agent_comms.installer.probes.carry import Carried
from agent_comms.pi_native_backend import PiNativeBackend
from agent_comms.registry_store import RegistryStore
from agent_comms.session_file import BusSessionLocation, ExternalSessionLocation, RegistrySessionLocation
from conftest import maintenance_held
from cutover.carry_session_locations import SessionLocations
from agent_comms.threads import Thread


def old_registry(tmp_path):
    comms = wire(tmp_path / "wire")
    bus = comms.root / "native-sessions" / "bus.jsonl"
    external = tmp_path / "harness" / "external.jsonl"
    for name, path in (("bus", bus), ("harness", external)):
        comms.registry.declare(Thread(
            name, frozenset(), str(tmp_path),
            session_file=RegistrySessionLocation.from_path(path, comms.root)))
    raw = json.loads(comms.registry.store.path.read_text())
    raw["threads"]["bus"]["session_file"] = str(bus)
    raw["threads"]["harness"]["session_file"] = str(external)
    comms.registry.store.path.write_text(json.dumps(raw, indent=2))
    return comms, bus, external


def test_carry_splits_bus_and_external_transcripts_once(tmp_path):
    comms, bus, external = old_registry(tmp_path)
    step = SessionLocations(comms.root, comms.root, tmp_path / "spill")
    with maintenance_held(comms.root):
        counts = {"BusSessionLocation": 1, "ExternalSessionLocation": 1}
        assert step.carry(apply=False) == Carried(2, counts)
        assert step.carry(apply=True).detail == counts
        assert step.carry(apply=False) == Carried(0)
    stored = json.loads(comms.registry.store.path.read_text())["threads"]
    assert stored["bus"]["session_file"] == {"path": "native-sessions/bus.jsonl"}
    published = {"path": str(external), "published": str(comms.root.resolve())}
    assert stored["harness"]["session_file"] == published

    copy = tmp_path / "copy"
    shutil.copytree(comms.root, copy, symlinks=True)
    copied = RegistryStore.at(copy).read_snapshot_unlocked()
    assert copied.require("bus").runtime.path == str(copy.resolve() / "native-sessions" / "bus.jsonl")
    assert copied.require("harness").runtime.path == str(external)
    with pytest.raises(RelationViolationError, match="cannot write external session"):
        copied.require("harness").writable_runtime
    assert copied.require("bus").writable_runtime.path.startswith(str(copy.resolve()))


def test_unbound_location_refuses_to_guess_its_root():
    for location in (BusSessionLocation(Path("native-sessions/a.jsonl")),
                     ExternalSessionLocation(Path("/harness/a.jsonl"), Path("/bus"))):
        with pytest.raises(RelationViolationError, match="must be bound by its registry"):
            location.location(PiNativeBackend)


def test_publishing_into_another_root_keeps_the_named_transcript(tmp_path):
    first = RegistrySessionLocation.from_path(tmp_path / "a" / "s.jsonl", tmp_path / "a")
    moved = first.bound_to(tmp_path / "b")
    assert isinstance(moved, ExternalSessionLocation)
    assert moved.location(PiNativeBackend).path == str((tmp_path / "a" / "s.jsonl").resolve())
    assert moved.published == (tmp_path / "a").resolve()
    with pytest.raises(RelationViolationError, match="published by"):
        moved.writable_location(PiNativeBackend)


def test_a_private_root_writes_the_harness_transcript_it_published(tmp_path):
    comms = wire(tmp_path / "private")
    harness = tmp_path / "harness" / "external.jsonl"
    comms.registry.declare(Thread(
        "owner", frozenset(), str(tmp_path),
        session_file=RegistrySessionLocation.from_path(harness, comms.root)))
    assert comms.registry.require("owner").writable_runtime.path == str(harness.resolve())


def test_carry_converts_the_archived_catalogue_before_reading_its_roots(tmp_path):
    old = wire(tmp_path / "old")
    session = old.root / "native-sessions" / "bus.jsonl"
    old.registry.declare(Thread(
        "bus", frozenset(), str(tmp_path), session_file=RegistrySessionLocation.from_path(session, old.root)))
    Bus.install(Bus.path_in(old.root))
    live = wire(tmp_path / "live")
    Bus.install(Bus.path_in(live.root))
    live.views.attach_history(old.root)
    catalog = HistoryArchive.path_in(live.root)
    recorded = json.loads(catalog.read_text())
    recorded[0]["provenance"]["threads"]["bus"]["session_file"] = str(session)
    catalog.write_text(json.dumps(recorded))
    step = SessionLocations(live.root, live.root, tmp_path / "spill")
    with maintenance_held(live.root):
        assert step.carry(apply=False) == Carried(1, {"BusSessionLocation": 1})
        assert step.carry(apply=True).changed == 1
        assert step.carry(apply=False) == Carried(0)
    (source,) = live.bus.history.sources()
    assert source.provenance.require("bus").runtime.path == str(session.resolve())
