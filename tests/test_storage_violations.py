"""Storage facts retain separate identity through discovery and heartbeat rendering."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from sys import monitoring
from types import CodeType

import pytest

from agent_comms.change_report import FileLine
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import OrderCheck
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.standing_rules import StandingRule
from agent_comms.storage_rules import (
    StorageHomeMissing,
    StorageOverQuota,
    StorageRule,
    StorageViolation,
    UnitStorage,
)
from agent_comms.todos import GoalRef, MechanicalOrderRun, PendingProblemOrder


@pytest.mark.parametrize(
    "has_home,extra_count,expected",
    [
        (False, 0, {StorageHomeMissing}),
        (True, 3, {StorageOverQuota}),
        (False, 3, {StorageHomeMissing, StorageOverQuota}),
    ],
    ids=("home-missing-only", "quota-only", "quota-and-home-missing"),
)
def test_storage_family_reads_once_and_renders_each_broken_fact(
    tmp_path: Path,
    has_home: bool,
    extra_count: int,
    expected: set[type[StorageViolation]],
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    base = tmp_path / "checkouts"
    base.mkdir()
    home = base / "worker"
    if has_home:
        home.mkdir()
    for arguments in (
        ("init", "-q"),
        (
            "-c",
            "user.name=Storage fixture",
            "-c",
            "user.email=storage@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "Storage fixture",
        ),
        *(
            ("worktree", "add", "--detach", "-q", str(base / f"worker--P{index + 1}"))
            for index in range(extra_count)
        ),
    ):
        subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    (root / "storage.json").write_text(
        json.dumps(FieldCodec.encode(StorageRule(checkout_base=base)))
    )
    comms = Comms(root)
    lead = comms.registry.declare(comms.registry.new_thread("lead", frozenset(), str(repository)))
    worker = comms.registry.declare(
        comms.registry.new_thread("worker", frozenset(), str(repository))
    )
    comms.goals.todos.create(
        "storage-case",
        RepositoryName.from_wire("OpenHCSDev/agent-comms"),
        "Fix the filesystem paths",
        creator=lead,
        owner=worker,
        generation="storage-case",
        review_at=RecordedEventTime(1000),
        goal=GoalRef("lead", lead.created_at, "storage-case"),
        order=PendingProblemOrder(
            id="storage-case-order",
            owner=FileLine(__file__, 1),
            run=MechanicalOrderRun(),
            check=OrderCheck(str(tmp_path), "true"),
            edits=("Fix the filesystem paths",),
            banned=("No extra store",),
            report="Report the delivered commit",
            at=RecordedEventTime(10),
            first_call_deadline=RecordedEventTime(130),
        ),
    )
    reads: list[UnitStorage] = []

    def observed_read(_code: CodeType, _offset: int, value: UnitStorage) -> None:
        reads.append(value)

    monitoring.use_tool_id(monitoring.PROFILER_ID, __name__)
    try:
        monitoring.register_callback(
            monitoring.PROFILER_ID, monitoring.events.PY_RETURN, observed_read
        )
        monitoring.set_local_events(
            monitoring.PROFILER_ID, UnitStorage.read.__func__.__code__, monitoring.events.PY_RETURN
        )
        found = StorageViolation.broken(comms, "lead")
    finally:
        monitoring.free_tool_id(monitoring.PROFILER_ID)
    assert {type(violation) for violation in found} == expected
    assert len(reads) == 1
    assert all(violation.storage is reads[0] for violation in found)
    for violation in found:
        text = violation.line()
        if isinstance(violation, StorageHomeMissing):
            assert "quota" not in text.lower()
            assert str(home) in text
            assert "restore the home checkout" in text
        else:
            assert "3 checkouts" in text and "limit 2" in text
            assert "order the cleanup" in text
            assert "missing" not in text
    print("\n".join(StandingRule.heartbeat_changes((), found)))
