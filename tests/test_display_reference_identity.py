"""A UI builds its display references on every registry revision, so they decode no thread row."""
import shutil
from pathlib import Path

import pytest

from agent_comms.display_references import CurrentDisplayReferences
from agent_comms.registry.registration import Registration
from agent_comms.repo import Checkout
from agent_comms.registry.registry_document import ThreadRows
from agent_comms.registry.threads import ExternalThread


def test_references_name_every_identity_without_decoding_a_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = Registration(tmp_path / "written" / "registry.json")
    for name in ("alpha", "beta"):
        written.declare(ExternalThread(name, frozenset(), Checkout(tmp_path)))
    written.rename("beta", "beta-renamed")
    expected = {name: thread.incarnation for name, thread in written.snapshot().named_members()}

    copied = tmp_path / "copied" / "registry.json"
    copied.parent.mkdir()
    shutil.copy(written.store.path, copied)

    def decoded(_rows: ThreadRows, _row: object) -> None:
        raise AssertionError("building display references decoded a thread row")

    monkeypatch.setattr(ThreadRows, "decode_row", decoded)
    references = CurrentDisplayReferences.from_registry(str(tmp_path), Registration(copied).snapshot())
    assert dict(references.members) == expected
