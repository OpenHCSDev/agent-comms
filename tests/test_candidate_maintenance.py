"""Private candidate maintenance is post-commit, optional, and never authority."""

from __future__ import annotations

from conftest import publish_initial
from agent_comms.read_basis import NamedViewer
import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import agent_comms.candidate_maintenance as maintenance
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from agent_comms.wake_candidate_index import (
    WakeCandidateIndex, FirstBuild,
)
from agent_comms.wake_policy import FullWake

_SCHEDULE = maintenance.schedule_candidate_catchup

pytestmark = pytest.mark.skipif(os.name != "posix", reason="private bus requires POSIX")


def _wire(base: Path) -> tuple[Comms, str]:
    root = base / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    comms.registry.declare(
        comms.registry.new_thread('sender', frozenset(), str(base), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    comms.registry.declare(
        comms.registry.new_thread('beta', frozenset({'team'}), str(base), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    return comms, comms.messaging.initialize_private_initial_protocol()


