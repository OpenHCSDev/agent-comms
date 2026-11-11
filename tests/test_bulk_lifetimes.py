"""Paused source I/O and state cleanup never lend a registry writer to bulk work."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import ClassVar

import pytest
from conftest import installed_comms

from agent_comms.cli_bridges.history import History, OfflineHistory
from agent_comms.cli_verbs.restore_owner import RestoreOwnerCliCommand
from agent_comms.errors import RelationViolationError
from agent_comms.execution.imported_conversation import KeptPiSession
from agent_comms.execution.imported_messages import ImportLimits
from agent_comms.execution.session_file import RegistrySessionLocation
from agent_comms.execution.imported_conversation import KeptPiSession
from agent_comms.installer.harness_install import Harness, PiHarness
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_status import StoppedThreadStatus
from agent_comms.registry.thread_text import AnySession
from agent_comms.registry.threads import ExternalThread, NativeThread


@dataclass(frozen=True, slots=True, kw_only=True)
class PausedPiSession(KeptPiSession):
    entered: Event
    proceed: Event

    def session_bytes(self, project: Path) -> bytes:
        self.entered.set()
        if not self.proceed.wait(5):
            raise TimeoutError("The controlled source copy was not released")
        return super(PausedPiSession, self).session_bytes(project)


@dataclass
class PreparedHistory(History, declared_name=None):
    snapshot: PausedPiSession
    harness: ClassVar[type[Harness]] = PiHarness

    def history(self):
        return OfflineHistory.stored(PiHarness, self.snapshot.source, AnySession()).history()

    def imported(self, limits: ImportLimits) -> PausedPiSession:
        return self.snapshot


@pytest.fixture
def paused_import(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_bytes(b'{"type":"session","version":3,"id":"original","cwd":"/original"}\n')
    snapshot = PausedPiSession(
        harness=PiHarness, source_id="original", project=str(tmp_path),
        messages_seen=0, source=source, entered=Event(), proceed=Event(),
    )
    yield PreparedHistory(snapshot)
    snapshot.proceed.set()


@pytest.mark.parametrize("collision", [False, True])
def test_import_source_copy_allows_other_writers_and_rechecks_name_before_publish(
    tmp_path, paused_import, collision,
):
    comms = installed_comms(tmp_path / "wire")
    source = paused_import.snapshot.source.read_bytes()
    with ThreadPoolExecutor(max_workers=2) as workers:
        imported = workers.submit(comms.threads.import_thread, paused_import, name="copied")
        try:
            assert paused_import.snapshot.entered.wait(5)
            name = "copied" if collision else "unrelated"
            competitor = workers.submit(comms.registry.declare, NativeThread(
                name, frozenset(), Checkout(tmp_path)))
            declared = competitor.result(timeout=2)
        finally:
            paused_import.snapshot.proceed.set()
        if collision:
            with pytest.raises(RelationViolationError):
                imported.result(timeout=5)
            assert comms.registry.require("copied") == declared
            assert not tuple((comms.root / "imported_sessions").iterdir())
        else:
            result = imported.result(timeout=5)
            assert Path(result.session_file).read_bytes() == source
            assert comms.registry.require("unrelated") == declared
    assert paused_import.snapshot.source.read_bytes() == source


def test_rehome_refuses_a_changed_original_before_publishing_the_staged_copy(
    tmp_path, paused_import,
):
    comms = installed_comms(tmp_path / "wire")
    original = comms.registry.declare(ExternalThread(
        "external", frozenset(), Checkout(tmp_path)))
    changed_project = tmp_path / "changed"
    changed_project.mkdir()
    with ThreadPoolExecutor(max_workers=2) as workers:
        rehomed = workers.submit(comms.threads.rehome_thread, paused_import, name=original.name)
        try:
            assert paused_import.snapshot.entered.wait(5)
            moved = workers.submit(comms.threads.set_project, original.incarnation, str(changed_project))
            moved.result(timeout=2)
        finally:
            paused_import.snapshot.proceed.set()
        with pytest.raises(RelationViolationError):
            rehomed.result(timeout=5)
    current = comms.registry.require(original.name)
    assert type(current) is ExternalThread
    assert current.project_path == changed_project
    assert not tuple((comms.root / "imported_sessions").iterdir())


def test_deleting_remains_fenced_while_cleanup_allows_unrelated_registry_writers(tmp_path):
    comms = installed_comms(tmp_path / "wire")
    session = tmp_path / "victim.jsonl"
    session.write_bytes(b"saved original")
    original = comms.registry.declare(NativeThread(
        "victim", frozenset(), Checkout(tmp_path),
        session_file=RegistrySessionLocation.from_path(str(session), comms.root)),
        StoppedThreadStatus())
    comms.ledger.merge({"victim": "remove this owned value"}, author=original.name)
    with ThreadPoolExecutor(max_workers=2) as workers:
        with comms.ledger.guarded_write():
            deleted = workers.submit(comms.threads.delete, original.name)
            deadline = time.monotonic() + 2
            while comms.registry.snapshot().status(original.name).declared_name != "deleting":
                assert time.monotonic() < deadline
                time.sleep(.01)
            other = workers.submit(comms.registry.declare, NativeThread(
                "unrelated", frozenset(), Checkout(tmp_path)))
            other.result(timeout=2)
            selected = comms.registry.require(original.name)
            assert not RestoreOwnerCliCommand.available_for_thread(
                selected, comms.registry.status(original.name))
            for operation in (comms.registry.unregister, comms.registry.archive,
                              comms.registry.remove, comms.owners.stop):
                with pytest.raises(RelationViolationError):
                    operation(original.name)
            with pytest.raises(RelationViolationError):
                comms.registry.register(original)
            with pytest.raises(RelationViolationError):
                comms.threads.delete(original.name)
            assert session.read_bytes() == b"saved original"
        result = deleted.result(timeout=5)
    assert result.deleted == original.name
    assert original.name not in comms.registry
    assert comms.registry.require("unrelated").project_path == tmp_path
    assert not session.exists()
