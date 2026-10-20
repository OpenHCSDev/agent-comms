"""Deferred, bounded candidate indexing never becomes delivery/native authority."""

from __future__ import annotations

from conftest import publish_initial
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.threads import Thread
from agent_comms.wake_candidate_index import (
    CandidateCatchUp, CandidatePage, CommittedAppendHint, ProjectionRebuildRequiredError, ProjectionUnavailableError, WakeCandidateIndex, FirstBuild, RebuildIndex, ResumeIndex,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="private bus requires POSIX")


def _fresh(tmp_path: Path, *, recipients: int = 1) -> tuple[Comms, WakeCandidateIndex, str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    comms.registry.declare(
        comms.registry.new_thread('sender', frozenset({'writer'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), created_at=1700010000.0)
    )
    for number in range(recipients):
        comms.registry.declare(
            comms.registry.new_thread(f'member{number:03}', frozenset({'cohort'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), created_at=1700010100.0 + number)
        )
    root_id = comms.messaging.initialize_private_initial_protocol()
    return comms, WakeCandidateIndex(comms.bus.log), root_id, RecordedThreadIncarnation("fixture", 1_700_010_100.0).lookup


def _page(
    index: WakeCandidateIndex, root_id: str, lookup: str, through: int, *, after: int = 0
) -> CandidatePage:
    return index.page(
        root_id=root_id,
        recipient_lookup=lookup,
        after_seq=after,
        required_through_seq=through,
        limit=100,
    )


