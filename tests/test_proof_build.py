"""Build-bound close uses real repositories and a retained-history bus copy."""


import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agent_comms.build_identity import (
    DeliveredCommitNotInstalledError,
    ProofBuildMismatchError,
    RecordedBuildIdentity,
    UninstalledRepositoryError,
)
from agent_comms.check_root import CheckRoot
from agent_comms.commander.order import Commander
from agent_comms.commander.standing import InstallDue
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec, GoalId
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.installer.probes.packages import ArchiveRecord, CommitRecord, GitRecord, PackageRecord
from agent_comms.installer.runtime_resolution import CorePackage, RuntimePackage, RuntimeResolution
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.order_runs import ApplicationOrderRun, MechanicalOrderRun
from agent_comms.progress_claims import WorkingClaim
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import GitCommandError, Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.standing_rules import StandingRule
from agent_comms.todos import (
    CommitDelivery,
    DoneTodoState,
    FileDeliveryEvidence,
    OpenTodoState,
    OperationDelivery,
    PendingProblemOrder,
    Todo,
)
from agent_comms.tool_caller import ThreadCaller
from agent_comms.order_tools import CommsOrderDoneTool, CommsProblemCloseTool, CommsTodoCreateTool
from fixture_git import IDENTITY
from order_scope_fixture import FIX_SCOPE
from source_revision_fixture import SourceRepository
from agent_comms.threads import Thread


@dataclass(frozen=True)
class ProofBuildCheckout:
    repository: SourceRepository
    oldest: str
    installed: str
    newer: str


@pytest.fixture
def proof_build_checkouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ProofBuildCheckout, ...]:
    """Own each package's distinct commit graph and the installed manifest."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    checkouts = []
    packages = {}
    for package in RuntimePackage.members_with(RuntimePackage):
        source = SourceRepository.create(tmp_path / 'source' / package.declared_name, package.repository)
        checkout = source.checkout
        commits = []
        for number in range(3):
            subprocess.run(
                (
                    "git",
                    *IDENTITY,
                    "commit",
                    "--allow-empty",
                    "-m",
                    f"{package.repository.to_wire()} proof {number}",
                ),
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            commits.append(
                subprocess.check_output(
                    ("git", "rev-parse", "HEAD"), cwd=checkout, text=True
                ).strip()
            )
        graph = ProofBuildCheckout(source, *commits)
        subprocess.run(('git', 'push', 'origin', 'main'), cwd=checkout, check=True, capture_output=True)
        checkouts.append(graph)
        packages[package.distribution] = PackageRecord(
            version="1",
            source=GitRecord(
                url=f"https://github.com/{package.repository.to_wire()}",
                vcs_info=CommitRecord(commit_id=graph.installed),
            ),
        )
    core = next(graph for graph in checkouts if graph.repository.github == CorePackage.repository)
    packages[CorePackage.distribution] = PackageRecord(
        version="1", source=ArchiveRecord(url="file:///owned/core-wheel.whl")
    )
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python").symlink_to(sys.executable)
    RuntimeResolution(core_commit=core.installed, packages=packages).save(runtime)
    current = home / ".local/share/agent-comms/current"
    current.parent.mkdir(parents=True)
    current.symlink_to(runtime, target_is_directory=True)
    return tuple(checkouts)


@pytest.mark.parametrize("run, after_install", (
    (MechanicalOrderRun(), DoneTodoState),
    (ApplicationOrderRun("Open the installed application and confirm the retained order is working"),
     OpenTodoState),
))
def test_merge_in_head_cannot_close_until_the_recorded_build_contains_it(
    run, after_install, proof_build_checkouts, comms, tmp_path,
):
    fixture_goal_id = GoalId.mint()
    from agent_comms.launch_runtime import LaunchRuntime
    from test_todos import order, thread

    graph = next(graph for graph in proof_build_checkouts
                 if graph.repository.github == CorePackage.repository)
    checkout = graph.repository.checkout
    subprocess.run(("git", "merge-base", "--is-ancestor", graph.newer, "HEAD"),
                   cwd=checkout, check=True)
    subprocess.run(("git", "merge-base", "--is-ancestor", graph.newer, "origin/main"),
                   cwd=checkout, check=True)
    lead = thread("lead", comms.root, 1.0)
    comms.registry.register(lead)
    merged = replace(order(graph.repository), run=run).merged(graph.newer)
    todo = comms.goals.todos.create(
        "P1", graph.repository.github, "Prove installation instead of inferring it from merge",
        creator=lead, assigner=lead.incarnation, owner=lead, generation=fixture_goal_id,
        request=comms.goals.todos.add_request(lead.incarnation, "Prove the installed build").id,
        review_at=RecordedEventTime(1000), order=merged,
    )
    with pytest.raises(DeliveredCommitNotInstalledError):
        merged.require_installation()
    observed = run.after_merge(todo, merged)
    assert observed == todo  # Waiting again does not manufacture a revision.
    assert observed.state is OpenTodoState
    assert not observed.order.accepted
    artifact = tmp_path / "application-proof.txt"
    works = "Open the installed application and confirm the retained order is working"
    artifact.write_text(works)
    with pytest.raises(DeliveredCommitNotInstalledError):
        ThreadCaller("lead").invoke(CommsProblemCloseTool(
            problem=todo.id, works=works, artifact=str(artifact),
        ), comms)
    assert comms.goals.todos.get(todo.id) == todo

    runtime = LaunchRuntime.installed()
    resolution = RuntimeResolution.at(runtime.prefix)
    RuntimeResolution(core_commit=graph.newer, packages=resolution.packages).save(runtime.prefix)
    accepted = run.after_merge(todo, merged)
    assert accepted.state is after_install
    assert merged.waiting_on_install(todo, {}) == (todo,)
    ThreadCaller("lead").invoke(CommsProblemCloseTool(
        problem=todo.id, works=works, artifact=str(artifact),
    ), comms)
    closed = comms.goals.todos.get(todo.id)
    assert closed.state is DoneTodoState
    assert closed.order.accepted
    assert FieldCodec.decode(Todo, FieldCodec.encode(closed)) == closed


def test_runtime_package_family_owns_installed_commits(proof_build_checkouts):
    installed = RecordedBuildIdentity.installed()
    members = RuntimePackage.members_with(RuntimePackage)
    assert len(installed.commits) == len(members) == len(proof_build_checkouts)
    for package, recorded in zip(members, installed.commits, strict=True):
        graph = next(
            graph
            for graph in proof_build_checkouts
            if graph.repository.github == package.repository
        )
        assert recorded.repository == package.repository
        assert recorded.commit == graph.installed
    assert FieldCodec.decode(RecordedBuildIdentity, FieldCodec.encode(installed)) == installed


@pytest.mark.parametrize("package", RuntimePackage.members_with(RuntimePackage))
def test_containment_selects_the_checkouts_installed_repository(package, proof_build_checkouts):
    installed = RecordedBuildIdentity.installed()
    graph = next(
        graph for graph in proof_build_checkouts if graph.repository.github == package.repository
    )
    assert installed.contains(graph.oldest, graph.repository.checkout)
    assert installed.contains(graph.installed, graph.repository.checkout)
    assert not installed.contains(graph.newer, graph.repository.checkout)


def test_containment_refuses_an_uninstalled_repository(tmp_path, proof_build_checkouts):
    checkout = tmp_path / "uninstalled"
    subprocess.run(("git", "init", str(checkout)), check=True, capture_output=True)
    name = RepositoryName("Example", "uninstalled")
    subprocess.run(
        ("git", "remote", "add", "origin", f"https://github.com/{name.to_wire()}.git"),
        cwd=checkout,
        check=True,
        capture_output=True,
    )
    with pytest.raises(UninstalledRepositoryError) as refusal:
        RecordedBuildIdentity.installed().contains(proof_build_checkouts[0].installed, checkout)
    assert name.to_wire() in str(refusal.value)
    assert str(checkout) in str(refusal.value)


def test_installed_proof_closes_and_older_build_is_refused_on_copied_bus(
    tmp_path, proof_build_checkouts
):
    graph = next(
        graph
        for graph in proof_build_checkouts
        if graph.repository.github == CorePackage.repository
    )
    source = wire(tmp_path / "bus")
    for number, name in enumerate(("lead", "worker"), 1):
        source.registry.declare(
            Thread(
                name, frozenset({"comms"}), str(tmp_path), created_at=float(number)
            )
        )
    installed = RecordedBuildIdentity.installed()
    selected = installed.for_repository(graph.repository)
    older = replace(
        installed,
        commits=tuple(
            replace(record, commit=graph.oldest) if record == selected else record
            for record in installed.commits
        ),
    )
    checkout = graph.repository.checkout
    checked, contained, missing = graph.newer, graph.installed, graph.newer
    caller = ThreadCaller("lead")
    works = "Open the installed application and confirm the copied goal keeps its source location."
    artifact = tmp_path / "proof.txt"
    artifact.write_text(works)
    source.registry.storage_rule = replace(source.registry.storage_rule, scratch_base=tmp_path)
    with CheckRoot(source, ("lead", "worker"), tmp_path / "proof" / "copied").open() as copied:
        request = copied.goals.todos.add_request(
            copied.registry.require("lead").incarnation, "Record the copied application proof",
        ).id
        cases = (
            ("P1", OperationDelivery(proof_build=installed), None),
            ("P2", OperationDelivery(proof_build=older), ProofBuildMismatchError),
            (
                "P3",
                CommitDelivery(commit=checked, delivered_commit=contained, proof_build=installed),
                None,
            ),
            (
                "P4",
                CommitDelivery(commit=checked, delivered_commit=missing, proof_build=installed),
                DeliveredCommitNotInstalledError,
            ),
        )
        for problem, delivery, expected_error in cases:
            build = delivery.proof_build
            order = PendingProblemOrder(
                scope=FIX_SCOPE,
                owner=FIX_SCOPE.shape.parents[0],
                check=OrderCheck(str(checkout), "printf 0", CountCheckResult(0)),
                edits=("Record the copied application proof on its installed build.",),
                banned=("Do not change the source bus.",),
                report="Report the actual close result.",
                run=ApplicationOrderRun(works),
            )
            caller.invoke(
                CommsTodoCreateTool(
                    owner="worker",
                    order=CreateProblemGoalAction(
                        problem=problem,
                        order=order,
                        request=request,
                        review_at=RecordedEventTime(4102444800),
                        commander="lead", assigner=caller.identity(copied),
                        repo=graph.repository.github,
                        text="Bind proof to its build",
                    ),
                ),
                copied,
            )
            caller.invoke(
                CommsOrderDoneTool(
                    problem=problem,
                    order_id=order.id,
                    delivery=replace(delivery, evidence=FileDeliveryEvidence(str(artifact))),
                    claims=(WorkingClaim(str(artifact)),),
                ),
                copied,
            )
            delivered = copied.goals.todos.get(problem)
            assert delivered.order.delivery.proof_build == build
            assert FieldCodec.decode(Todo, FieldCodec.encode(delivered)) == delivered
            close = CommsProblemCloseTool(problem=problem, works=works, artifact=str(artifact))
            if expected_error is None:
                caller.invoke(close, copied)
                saved = copied.goals.todos.get(problem)
                assert saved.order.accepted and not saved.state.open
            else:
                with pytest.raises(expected_error) as refusal:
                    caller.invoke(close, copied)
                assert missing in str(refusal.value) or str(older) in str(refusal.value)
                assert str(installed) in str(refusal.value)
                assert copied.goals.todos.get(problem) == delivered
                print(f"{type(refusal.value).__name__}: {refusal.value}")
    assert not source.goals.todos.for_commander(source.registry.require("lead").incarnation)


def test_build_containment_does_not_turn_git_failure_into_absence_or_success(proof_build_checkouts):
    graph = proof_build_checkouts[0]
    with pytest.raises(GitCommandError):
        RecordedBuildIdentity.installed().contains("0" * 40, graph.repository.checkout)


@pytest.mark.parametrize("advance_main", (False, True), ids=("installed-main", "newer-main"))
def test_install_due_compares_fetched_main_to_installed_build(
    tmp_path, proof_build_checkouts, advance_main, monkeypatch,
):
    graph = next(graph for graph in proof_build_checkouts
                 if graph.repository.github == CorePackage.repository)
    checkout = graph.repository.checkout
    # A real local origin retains the repository identity Repo.of derives from its URL.
    origin = tmp_path / graph.repository.github.owner / f"{graph.repository.github.name}.git"
    origin.parent.mkdir()
    subprocess.run(("git", "init", "--bare", str(origin)), check=True, capture_output=True)
    Repo.git(checkout, "remote", "set-url", "origin", str(origin))
    Repo.git(checkout, "push", "origin", f"{graph.installed}:refs/heads/main")
    if advance_main:
        Repo.git(checkout, "push", "origin", f"{graph.newer}:refs/heads/main")
    # Deliberately stale: the rule must fetch rather than trust the existing remote ref.
    Repo.git(checkout, "update-ref", "refs/remotes/origin/main", graph.oldest)
    before = Repo.git(checkout, "rev-parse", "HEAD")
    bus = wire(tmp_path / "bus")
    bus.registry.declare(Thread("lead", frozenset(), str(checkout)))
    Commander.on_root("lead", (graph.repository,), bus.root)

    after_settle = time.time() + InstallDue.settle + 1
    monkeypatch.setattr(time, "time", lambda: after_settle)
    due = InstallDue.violations(bus, "lead")

    assert Repo.git(checkout, "rev-parse", "HEAD") == before
    assert Repo.git(checkout, "rev-parse", "origin/main").strip() == (
        graph.newer if advance_main else graph.installed)
    assert len(due) == int(advance_main)
    for alarm in due:
        assert alarm.repo == graph.repository.github
        assert alarm.main == graph.newer
        assert alarm.installed == graph.installed

    # An install advances the canonical manifest, not a clock or bus schedule.
    runtime = LaunchRuntime.installed().prefix
    resolution = RuntimeResolution.at(runtime)
    RuntimeResolution(core_commit=graph.newer, packages=resolution.packages).save(runtime)
    after_install = InstallDue.violations(bus, "lead")
    assert after_install == ()
    assert Repo.git(checkout, "rev-parse", "HEAD") == before


def test_install_waits_for_merges_to_settle_into_one_batch(
    tmp_path, proof_build_checkouts, monkeypatch,
):
    """A merge younger than the settle window is not due yet; once it has stood, it is."""
    graph = next(graph for graph in proof_build_checkouts
                 if graph.repository.github == CorePackage.repository)
    checkout = graph.repository.checkout
    origin = tmp_path / graph.repository.github.owner / f"{graph.repository.github.name}.git"
    origin.parent.mkdir()
    subprocess.run(("git", "init", "--bare", str(origin)), check=True, capture_output=True)
    Repo.git(checkout, "remote", "set-url", "origin", str(origin))
    Repo.git(checkout, "push", "origin", f"{graph.newer}:refs/heads/main")
    bus = wire(tmp_path / "bus")
    bus.registry.declare(Thread("lead", frozenset(), str(checkout)))
    Commander.on_root("lead", (graph.repository,), bus.root)
    merged = int(Repo.git(checkout, "log", "-1", "--format=%ct", graph.newer))

    monkeypatch.setattr(time, "time", lambda: merged + InstallDue.settle - 1)
    assert InstallDue.violations(bus, "lead") == ()

    monkeypatch.setattr(time, "time", lambda: merged + InstallDue.settle)
    (due,) = InstallDue.violations(bus, "lead")
    assert due.main == graph.newer
    assert due.oldest_minutes == 30

