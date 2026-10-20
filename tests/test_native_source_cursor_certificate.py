"""checked source pages still require sealed and native owner-epoch proof."""

from __future__ import annotations

from conftest import publish_initial
import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

import agent_comms.coordinated_runtime as runtime
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.channels import ANY_CHANNEL
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.native_prompt_binding import install_prompt_binding_schema
from agent_comms.native_runtime_input import CurrentNativeCursor
from agent_comms.native_source_cursor import NativeSourceCursor
from agent_comms.threads import Thread
from agent_comms.tracked_turn import TrackedTurnSession
from test_native_prompt_binding import _fake_model


@pytest.fixture
def tmp_path():
    if os.name != "posix" or not Path("/var/tmp").is_dir() or Path("/var").is_symlink():
        pytest.skip("private sealed runtime needs a disposable /var/tmp")
    with tempfile.TemporaryDirectory(prefix="ac-checked-cursor-", dir="/var/tmp") as path:
        yield Path(path)


def _fresh(tmp_path: Path, count: int = 2):
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    comms.registry.declare(
        comms.registry.new_thread('sender', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    for n in range(count):
        name = "alpha" if n == 0 else f"other{n:03}"
        comms.registry.declare(
            comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), model='fake/fake')
        )
    root_id = comms.messaging.initialize_private_initial_protocol()
    with Coordination(str(root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        install_prompt_binding_schema(store)
        for n in range(count):
            name = "alpha" if n == 0 else f"other{n:03}"
            owner = comms.registry.require(name)
            store.participants.register(
                RecordedThreadIncarnation("fixture", owner.created_at).lookup, name, name, committed=True
            )
    return root, root_id, comms


def _seal(comms: Comms, root: Path, root_id: str, target: str, body: str):
    message = publish_initial(comms, "sender", target, body)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert accept_delivery_cohort(comms.bus, root_id, message.seq, store).value.member_count
    return message


