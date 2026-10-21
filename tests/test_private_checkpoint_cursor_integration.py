"""Producer/cursor integration beyond the legacy bus byte and page caps.

Provider-free native fake only: a source prefix record is not an ACK or input permit.
"""

from __future__ import annotations

from bus_record_fixture import prepare_native_comms

from conftest import publish_initial
import os
from pathlib import Path

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.native_prompt_binding import install_prompt_binding_schema
from agent_comms.native_runtime_input import CurrentNativeCursor
from agent_comms.native_source_cursor import NativeSourceCursor
from agent_comms.thread_model import DeclaredModel


@pytest.fixture
def tmp_path(private_directory):
    """Share the physical private root's joined retirement."""
    return private_directory


def _root(tmp_path: Path):
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = prepare_native_comms(Comms(root))
    people = [
        comms.registry.new_thread('sender', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())),
        comms.registry.new_thread('alpha', frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), model=DeclaredModel('openai-codex/gpt-6-sol')),
        comms.registry.new_thread('other', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())),
    ]
    for person in people:
        comms.registry.declare(person)
    root_id = comms.registry.prepare_native_root().namespace
    first = publish_initial(comms, "sender", "#team", "selected one")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        install_prompt_binding_schema(store)
        lookup = RecordedThreadIncarnation("fixture", people[1].created_at).lookup
        store.participants.register(lookup, "alpha", "alpha", committed=True)
        accept_delivery_cohort(comms.bus, root_id, first.seq, store)
    return root, root_id, comms, first, lookup


def test_checkpoint_index_rollback_denies_cursor_without_sql_mutation(tmp_path, *, source_reads):
    root, root_id, comms, first, lookup = _root(tmp_path)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(
                owner_name="alpha", source_reads=source_reads
            )
            is None
        )
    index = root / "private_bus_checkpoint.sqlite3"
    with __import__("sqlite3").connect(index) as db:
        db.execute("DELETE FROM addressed WHERE lookup=? AND seq=?", (lookup, first.seq))
    with Coordination(str(root / "coordination.sqlite3")) as reopened:
        with pytest.raises((RelationViolationError, IdentityConflict)):
            NativeSourceCursor(Comms(root).bus, reopened, wire_root_id=root_id).read(
                owner_name="alpha", source_reads=source_reads
            )
        assert (
            reopened.session._connection.execute(
                f"SELECT COUNT(*) FROM {CurrentNativeCursor.declared_name}"
            ).fetchone()[0]
            == 0
        )
