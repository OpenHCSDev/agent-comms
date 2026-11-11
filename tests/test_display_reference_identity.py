"""A UI builds its display references on every registry revision, so they decode no thread row."""
from pathlib import Path

import pytest

from agent_comms.messaging.display_references import CurrentDisplayReferences
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import StoredThreadRow
from agent_comms.registry.threads import ExternalThread
from agent_comms.storage.locked_store import RegistryFile


def test_references_name_every_identity_without_decoding_a_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = Registration(RegistryFile.path_in(tmp_path / "written"))
    for name in ("alpha", "beta"):
        written.declare(ExternalThread(name, frozenset(), Checkout(tmp_path)))
    written.rename("beta", "beta-renamed")
    expected = {name: thread.incarnation for name, thread in written.snapshot().named_members()}

    copied = RegistryFile.path_in(tmp_path / "copied")
    written.store.retain(copied.parent)

    def decoded(_row: StoredThreadRow) -> None:
        raise AssertionError("building display references decoded a thread row")

    monkeypatch.setattr(StoredThreadRow.thread, "func", decoded)
    references = CurrentDisplayReferences.from_registry(str(tmp_path), Registration(copied).snapshot())
    assert dict(references.members) == expected
