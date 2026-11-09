"""Storage facts retain separate identity through discovery and heartbeat rendering."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_comms.comms import wire
from agent_comms.nominal.field_codec import FieldCodec, GoalId
from agent_comms.planning.goals import OrderCheck
from agent_comms.planning.order_runs import MechanicalOrderRun
from agent_comms.planning.orders import (
    PendingProblemOrder,
    PresentCheckout,
    ProgressRead,
)
from agent_comms.projects.change_report import FileLine
from agent_comms.projects.checkouts import (
    Checkout,
    RepositoryReads,
)
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.storage_rules import (
    StorageRule,
    UnitStorage,
)
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.team.standing_rules import (
    StorageHomeMissing,
    StorageViolation,
)
from agent_comms.ui_model.teammate_state import TeammateState
from order_scope_fixture import FIX_SCOPE
from team_heartbeat_fixture import capture_frame


@pytest.mark.parametrize(
    "has_home,extra_count,expected",
    [
        (False, 0, {StorageHomeMissing}),
        (True, 3, set()),
        (False, 3, {StorageHomeMissing}),
        (True, 1, set()),
    ],
)
def test_storage_family_renders_each_broken_fact(
    tmp_path: Path,
    configured_repository,
    has_home: bool,
    extra_count: int,
    expected: set[type[StorageViolation]],
) -> None:
    storage_case_goal_id = GoalId.mint()
    repository = tmp_path / "repository"
    repository.mkdir()
    base = tmp_path / "checkouts"
    base.mkdir()
    home = tmp_path / "declared-project"
    for arguments in (
        ("init", "-q"),
        # A checkout's repository is its origin's (Repo.from_origin), like every real checkout.
        ("remote", "add", "origin", "https://github.com/OpenHCSDev/agent-comms.git"),
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
    colleague = comms.registry.declare(NativeThread("colleague", frozenset(), Checkout(repository)))
    configured_repository(comms)
    worker = comms.registry.declare(
        NativeThread("worker", frozenset(), Checkout(home))
    )
    comms.goals.todos.create(
        "P1",
        RepositoryName.from_wire("OpenHCSDev/agent-comms"),
        "Fix the filesystem paths",
        creator=colleague, assigner=colleague.incarnation,
        owner=worker,
        generation=storage_case_goal_id,
        review_at=RecordedEventTime(1000),
        request=comms.goals.todos.add_request(colleague.incarnation, "Fix the filesystem paths").id,
        order=PendingProblemOrder(
            scope=FIX_SCOPE,
            owner=FileLine(__file__, 1),
            run=MechanicalOrderRun(),
            check=OrderCheck(tmp_path, "true"),
            edits=("Fix the filesystem paths",),
            banned=("No extra store",),
            report="Report the delivered commit",
            at=RecordedEventTime(10),
            first_call_deadline=RecordedEventTime(130),
        ),
    )
    frame = capture_frame(comms, ProgressRead())
    found = StorageViolation.broken(
        comms, observed=TeammateState.current(comms, frame), frame=frame)
    storage = UnitStorage.read(comms.registry.storage_rule, worker, RepositoryReads())
    assert {type(violation) for violation in found} == expected
    assert all(violation.storage == storage for violation in found)
    assert storage.home.path == home
    assert len(storage.extra_checkouts) == extra_count
    assert storage.missing_home is not has_home
    assert not (base / "worker").exists()
    assert comms.registry.storage_rule.owns_present_checkout(worker, home, RepositoryReads()) is has_home
    assert not comms.registry.storage_rule.owns_present_checkout(worker, base / "worker", RepositoryReads())
    if has_home:
        declared_ref = subprocess.check_output(
            ["git", "-C", str(home), "symbolic-ref", "HEAD"], text=True, timeout=15
        ).strip()
        assert declared_ref in PresentCheckout(repository).references(comms.registry.storage_rule, worker)
    text = comms.registry.storage_rule.order_text(worker, ("python",))
    assert str(home) in text
    rule = comms.registry.storage_rule
    assert str(rule.order_checkout(rule.initial_roots(worker.name), "P<n>")) in text
    assert str(worker.storage_roots.scratch) in text
