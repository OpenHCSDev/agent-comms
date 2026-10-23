"""Storage facts retain separate identity through discovery and heartbeat rendering."""

from __future__ import annotations


import json
import shutil
import subprocess
from pathlib import Path
from sys import monitoring
from types import CodeType

import pytest

from agent_comms.change_report import FileLine
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import OrderCheck
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.order_runs import MechanicalOrderRun
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
from agent_comms.todos import PendingProblemOrder, PresentCheckout, ProgressRead
from agent_comms.ui_model.unit_state import UnitState
from order_scope_fixture import FIX_SCOPE
from agent_comms.order_runs import MechanicalOrderRun
from agent_comms.threads import Thread


@pytest.mark.parametrize(
    "has_home,extra_count,expected",
    [
        (False, 0, {StorageHomeMissing}),
        (True, 3, {StorageOverQuota}),
        (False, 3, {StorageHomeMissing, StorageOverQuota}),
        (True, 1, set()),
        (False, 1, {StorageHomeMissing}),
    ],
    ids=("home-missing-only", "quota-only", "quota-and-home-missing",
         "declared-home-with-order-checkout", "gone-declared-home-with-order-checkout"),
)
def test_storage_family_reads_once_and_renders_each_broken_fact(
    tmp_path: Path,
    configured_repository,
    has_home: bool,
    extra_count: int,
    expected: set[type[StorageViolation]],
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    base = tmp_path / "checkouts"
    base.mkdir()
    home = tmp_path / "declared-project"
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
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "-q", "-b", "declared-home", str(home)],
        check=True, capture_output=True, text=True, timeout=15,
    )
    if not has_home:
        shutil.rmtree(home)
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    (root / "storage.json").write_text(
        json.dumps(FieldCodec.encode(StorageRule(checkout_base=base)))
    )
    comms = wire(root)
    lead = comms.registry.declare(Thread("lead", frozenset(), str(repository)))
    configured_repository(comms)
    worker = comms.registry.declare(
        Thread("worker", frozenset(), str(home))
    )
    comms.goals.todos.create(
        "P1",
        RepositoryName.from_wire("OpenHCSDev/agent-comms"),
        "Fix the filesystem paths",
        creator=lead, assigner=lead.incarnation,
        owner=worker,
        generation="storage-case",
        review_at=RecordedEventTime(1000),
        request=comms.goals.todos.add_request(lead.incarnation, "Fix the filesystem paths").id,
        order=PendingProblemOrder(
            id="storage-case-order", scope=FIX_SCOPE,
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
        pulls = ProgressRead()
        found = StorageViolation.broken(
            comms, "lead", observed=UnitState.current(comms, "lead", pulls),
            frame=HeartbeatFrame.read(comms, "lead", pulls))
    finally:
        monitoring.free_tool_id(monitoring.PROFILER_ID)
    assert {type(violation) for violation in found} == expected
    assert len(reads) == 1
    assert all(violation.storage is reads[0] for violation in found)
    assert reads[0].home == home
    assert len(reads[0].extra_checkouts) == extra_count
    assert reads[0].missing_home is not has_home
    assert not (base / "worker").exists()
    assert comms.registry.storage_rule.owns_checkout(worker, home, home)
    assert not comms.registry.storage_rule.owns_checkout(worker, base / "worker", base / "worker")
    if has_home:
        declared_ref = subprocess.check_output(
            ["git", "-C", str(home), "symbolic-ref", "HEAD"], text=True, timeout=15
        ).strip()
        assert declared_ref in PresentCheckout(repository).references(comms.registry.storage_rule, worker)
    text = comms.registry.storage_rule.order_text(worker, ("python",))
    assert str(home) in text
    assert str(comms.registry.storage_rule.order_checkout(worker.name, "P<n>")) in text
    assert str(worker.storage_roots.scratch) in text
    print("\n".join(StandingRule.heartbeat_changes((), found)))
