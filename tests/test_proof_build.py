"""Build-bound close uses real repositories and a retained-history bus copy."""


import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import fixture_git
from agent_comms.comms import wire
from agent_comms.execution.check_root import CheckRoot
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.installer.build_identity import (
    DeliveredCommitNotInstalledError,
    ProofBuildMismatchError,
    RecordedBuildIdentity,
    UninstalledRepositoryError,
    UnrecordedBuildIdentity,
)
from agent_comms.installer.logs import InstallLogs
from agent_comms.installer.probes.interpreter import RecordedInterpreterBuild
from agent_comms.installer.probes.packages import (
    ArchiveRecord,
    CommitRecord,
    GitRecord,
    PackageRecord,
)
from agent_comms.installer.runtime_resolution import CorePackage, RuntimePackage, RuntimeResolution
from agent_comms.nominal.field_codec import FieldCodec, GoalId
from agent_comms.planning.delivery_evidence import FileDeliveryEvidence
from agent_comms.planning.goal_actions import CreateProblemGoalAction
from agent_comms.planning.goals import (
    CountCheckResult,
    OrderCheck,
)
from agent_comms.planning.held_orders import HeldOrders
from agent_comms.planning.order_runs import ApplicationOrderRun, MechanicalOrderRun
from agent_comms.planning.orders import (
    CheckedCommitDelivery,
    CommitDelivery,
    DoneTodoState,
    MergedListedPullRequest,
    OpenTodoState,
    OperationDelivery,
    PendingProblemOrder,
    ProgressRead,
    ReportDelivery,
    ReportedProblemOrder,
    Todo,
    UnmergedListedPullRequest,
)
from agent_comms.planning.progress_claims import WorkingClaim
from agent_comms.planning.pull_requests import (
    MainDeliveryMerge,
    PullRequestCommit,
    PullRequestLink,
)
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.git_errors import GitCommandError
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.work_branches import WorkBranches
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.team.order import Team
from agent_comms.team.standing import InstallDue
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tools.orders import (
    CommsOrderDoneTool,
    CommsProblemCloseTool,
    CommsTodoCreateTool,
)
from conftest import record_installed_runtime
from github_fixture import GitHubFixture
from order_scope_fixture import FIX_SCOPE
from source_revision_fixture import SourceRepository

pytest_plugins = ("github_fixture",)


@dataclass(frozen=True)
class ProofBuildCheckout:
    repository: SourceRepository
    oldest: str
    installed: str
    newer: str


@pytest.fixture
def proof_build_checkouts(
    tmp_path: Path, durable_home: Path, interpreter_build: RecordedInterpreterBuild,
) -> tuple[ProofBuildCheckout, ...]:
    """Own each package's distinct commit graph and the installed manifest; durable_home is the
    HOME the installation records under."""
    checkouts = []
    packages = {}
    for package in RuntimePackage.members_with(RuntimePackage):
        source = SourceRepository.create(tmp_path / 'source' / package.declared_name, package.repository)
        checkout = source.checkout
        commits = []
        for number in range(3):
            fixture_git.run(
                checkout, "commit", "--allow-empty", "-m", f"{package.repository.to_wire()} proof {number}")
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
    record_installed_runtime(
        tmp_path / "runtime", RuntimeResolution(
            core_commit=core.installed, packages=packages, interpreter=interpreter_build,
        ))
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
    from agent_comms.execution.launch_runtime import LaunchRuntime
    from test_todos import order, thread

    graph = next(graph for graph in proof_build_checkouts
                 if graph.repository.github == CorePackage.repository)
    checkout = graph.repository.checkout
    subprocess.run(("git", "merge-base", "--is-ancestor", graph.newer, "HEAD"),
                   cwd=checkout, check=True)
    subprocess.run(("git", "merge-base", "--is-ancestor", graph.newer, "origin/main"),
                   cwd=checkout, check=True)
    colleague = thread("colleague", comms.root, 1.0)
    comms.registry.register(colleague)
    merged = replace(order(graph.repository), run=run).merged(graph.newer)
    todo = comms.goals.todos.create(
        "P1", graph.repository.github, "Prove installation instead of inferring it from merge",
        creator=colleague, assigner=colleague.incarnation, owner=colleague, generation=fixture_goal_id,
        request=comms.goals.todos.add_request(colleague.incarnation, "Prove the installed build").id,
        review_at=RecordedEventTime(1000), order=merged,
    )
    with pytest.raises(DeliveredCommitNotInstalledError):
        merged.require_installation()
    assert merged.installed_line(RecordedBuildIdentity.installed()).startswith(
        "Merged, waiting on install")
    observed = run.after_merge(todo, merged)
    assert observed == todo  # Waiting again does not manufacture a revision.
    assert observed.state is OpenTodoState
    assert not observed.order.accepted
    artifact = tmp_path / "application-proof.txt"
    works = "Open the installed application and confirm the retained order is working"
    artifact.write_text(works)
    with pytest.raises(DeliveredCommitNotInstalledError):
        ThreadCaller("colleague").invoke(CommsProblemCloseTool(
            problem=todo.id, works=works, artifact=str(artifact),
        ), comms)
    assert comms.goals.todos.get(todo.id) == todo

    runtime = LaunchRuntime.installed()
    resolution = RuntimeResolution.at(runtime.prefix)
    resolution.model_copy(update={"core_commit": graph.newer}).save(runtime.prefix)
    accepted = run.after_merge(todo, merged)
    assert accepted.state is after_install
    # P553: bf328ceb5 sat in the installed build under "Merged, waiting on install".
    assert merged.installed_line(RecordedBuildIdentity.installed()).startswith(
        "Merged, installed")
    assert merged.waiting_on_install(todo, {}) == (todo,)
    ThreadCaller("colleague").invoke(CommsProblemCloseTool(
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
    assert installed.interpreter == RuntimeResolution.at(LaunchRuntime.installed().prefix).interpreter
    assert FieldCodec.decode(RecordedBuildIdentity, FieldCodec.encode(installed)) == installed


def test_same_commits_do_not_reuse_a_proof_from_another_interpreter(proof_build_checkouts):
    original = RecordedBuildIdentity.installed().recorded()
    runtime = LaunchRuntime.installed()
    resolution = RuntimeResolution.at(runtime.prefix)
    interpreter = resolution.interpreter.require_recorded()
    changed_image = interpreter.artifacts[0].model_copy(update={"sha256": "0" * 64})
    changed = interpreter.model_copy(update={
        "artifacts": (changed_image, *interpreter.artifacts[1:]),
    })
    resolution.model_copy(update={"interpreter": changed}).save(runtime.prefix)
    current = RecordedBuildIdentity.installed().recorded()
    assert original.commits == current.commits
    assert original != current
    assert FieldCodec.decode(RecordedBuildIdentity, FieldCodec.encode(current)) == current
    graph = proof_build_checkouts[0]
    with pytest.raises(ProofBuildMismatchError, match="differs from installed"):
        original.installation((), graph.repository.checkout)
    # Ancestry still answers its own question; it is not an interpreter proof.
    assert original.contains(graph.installed, graph.repository.checkout)
    assert current.contains(graph.installed, graph.repository.checkout)


def test_missing_interpreter_evidence_is_readable_but_cannot_certify_a_proof(proof_build_checkouts):
    installed = RecordedBuildIdentity.installed().recorded()
    unobserved = RecordedBuildIdentity(installed.commits)
    retained = FieldCodec.decode(RecordedBuildIdentity, FieldCodec.encode(unobserved))
    assert retained.commits == installed.commits
    with pytest.raises(ProofBuildMismatchError, match="no interpreter observation"):
        retained.recorded()
    with pytest.raises(ProofBuildMismatchError, match="no interpreter observation"):
        retained.installation((), proof_build_checkouts[0].repository.checkout)
    runtime = LaunchRuntime.installed()
    resolution = RuntimeResolution.at(runtime.prefix)
    resolution.model_copy(update={"interpreter": retained.interpreter}).save(runtime.prefix)
    with pytest.raises(ProofBuildMismatchError, match="no interpreter observation"):
        UnrecordedBuildIdentity().recorded()
    with pytest.raises(ProofBuildMismatchError, match="no interpreter observation"):
        installed.installation((), proof_build_checkouts[0].repository.checkout)


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
    tmp_path, proof_build_checkouts, github_cli: GitHubFixture
):
    graph = next(
        graph
        for graph in proof_build_checkouts
        if graph.repository.github == CorePackage.repository
    )
    source = wire(tmp_path / "bus")
    for number, name in enumerate(("colleague", "worker"), 1):
        source.registry.declare(
            NativeThread(
                name, frozenset({"comms"}), Checkout(tmp_path), created_at=float(number)
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
    checked, contained, missing = graph.installed, graph.installed, graph.newer
    github_cli.configure(graph.repository.github)
    for number, commit in ((1, checked), (2, checked), (3, contained), (4, missing)):
        row = MergedListedPullRequest(
            number, f'fix/{number}', commit, updated='2026-10-25T12:00:00Z', base=WorkBranches.main,
            reviewable_times=('2026-10-25T12:00:00Z',), merge=PullRequestCommit(commit),
            merged='2026-10-25T12:00:00Z')
        graph.repository.git(checkout, 'push', 'origin',
                             f'{row.head}:refs/pull/{row.number}/head')
        github_cli.put(graph.repository.github, row)
    caller = ThreadCaller("colleague")
    holder = ThreadCaller("worker")
    works = "Open the installed application and confirm the copied goal keeps its source location."
    artifact = tmp_path / "proof.txt"
    artifact.write_text(works)
    source.registry.storage_rule = replace(source.registry.storage_rule, scratch_base=tmp_path)
    with CheckRoot(source, ("colleague", "worker"), tmp_path / "proof" / "copied").open() as copied:
        request = copied.goals.todos.add_request(
            holder.identity(copied), "Record the copied application proof",
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
                CommitDelivery(commit=missing, delivered_commit=missing, proof_build=installed),
                DeliveredCommitNotInstalledError,
            ),
        )
        for number, (problem, delivery, expected_error) in enumerate(cases, 1):
            build = delivery.proof_build
            order = PendingProblemOrder(
                scope=FIX_SCOPE,
                owner=FIX_SCOPE.shape.parents[0],
                check=OrderCheck(checkout, "printf 0", CountCheckResult(0)),
                pull_requests=(PullRequestLink(graph.repository.github, number),),
                edits=("Record the copied application proof on its installed build.",),
                banned=("Do not change the source bus.",),
                report="Report the actual close result.",
                run=ApplicationOrderRun(works),
            )
            # The holder accepts each scenario; this tests proof binding, not
            # permission for a peer to replace the preceding active goal.
            holder.invoke(
                CommsTodoCreateTool(
                    owner="worker",
                    order=CreateProblemGoalAction(
                        problem=problem,
                        order=order,
                        request=request,
                        review_at=RecordedEventTime(4102444800),
                        assigner=holder.identity(copied),
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
    assert not source.goals.todos.board()


def test_build_containment_does_not_turn_git_failure_into_absence_or_success(proof_build_checkouts):
    graph = proof_build_checkouts[0]
    with pytest.raises(GitCommandError):
        RecordedBuildIdentity.installed().contains("0" * 40, graph.repository.checkout)


@pytest.mark.parametrize("advance_main", (False, True), ids=("installed-main", "newer-main"))
@pytest.mark.parametrize("extra_repositories", (False, True), ids=("runtime-only", "qualification-repos"))
def test_install_due_compares_fetched_main_to_installed_build(
    tmp_path, proof_build_checkouts, advance_main, extra_repositories, github_cli,
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
    bus.registry.declare(NativeThread("colleague", frozenset(), Checkout(checkout)))
    extras = tuple(SourceRepository.create(
        tmp_path / "qualification", RepositoryName("example", f"qualification-{number}"))
        for number in range(2 if extra_repositories else 0))
    for extra in extras:
        extra.git(extra.checkout, "commit", "--allow-empty", "-m", "Advance qualification-only main")
        extra.git(extra.checkout, "push", "origin", "main")
        # An install scan must not fetch even a deliberately stale extra source.
        extra.git(extra.checkout, "update-ref", "refs/remotes/origin/main", "main^")
        with pytest.raises(UninstalledRepositoryError):
            RecordedBuildIdentity.installed().for_repository(extra)
    registered = (graph.repository, *extras)
    Team.on_root("colleague", registered, bus.root)

    github_cli.configure(graph.repository.github)
    pulls = ProgressRead()
    InstallDue.prepare(bus, HeldOrders.read(bus), pulls)
    due = InstallDue.due(bus, pulls)

    assert Repo.git(checkout, "rev-parse", "HEAD") == before
    assert Repo.git(checkout, "rev-parse", "origin/main").strip() == (
        graph.newer if advance_main else graph.installed)
    assert len(due) == int(advance_main)
    for alarm in due:
        assert alarm.repo == graph.repository.github
        assert alarm.main == graph.newer
        assert alarm.installed == graph.installed
    for extra in extras:
        assert extra.git(extra.checkout, "rev-parse", "HEAD").strip() == extra.main_commit
        assert extra.git(extra.checkout, "rev-parse", "origin/main") == extra.git(
            extra.checkout, "rev-parse", "main^")
    assert InstallLogs(bus.root).units() == ()

    # An install advances the canonical manifest, not a clock or bus schedule.
    runtime = LaunchRuntime.installed().prefix
    resolution = RuntimeResolution.at(runtime)
    resolution.model_copy(update={"core_commit": graph.newer}).save(runtime)
    pulls = ProgressRead()
    InstallDue.prepare(bus, HeldOrders.read(bus), pulls)
    after_install = InstallDue.due(bus, pulls)
    assert after_install == ()
    assert Repo.git(checkout, "rev-parse", "HEAD") == before
    configured = bus.registry.snapshot().team_project.require().repos
    assert tuple(repo.github for repo in configured) == tuple(repo.github for repo in registered)
    assert tuple(repo.checkout for repo in configured) == tuple(repo.checkout for repo in registered)
    for extra in extras:
        assert extra.git(extra.checkout, "rev-parse", "HEAD").strip() == extra.main_commit
        assert extra.git(extra.checkout, "rev-parse", "origin/main") == extra.git(
            extra.checkout, "rev-parse", "main^")
    assert InstallLogs(bus.root).units() == ()


def test_a_one_minute_old_merge_is_due_and_names_its_request_without_launching(
    tmp_path, proof_build_checkouts, github_cli, monkeypatch,
):
    """Due is a disposition: the line names what an install would carry the pass main leaves
    the installed build, and the heartbeat's enforce launches nothing."""
    graph = next(graph for graph in proof_build_checkouts
                 if graph.repository.github == CorePackage.repository)
    checkout = graph.repository.checkout
    origin = tmp_path / graph.repository.github.owner / f"{graph.repository.github.name}.git"
    origin.parent.mkdir()
    subprocess.run(("git", "init", "--bare", str(origin)), check=True, capture_output=True)
    Repo.git(checkout, "remote", "set-url", "origin", str(origin))
    Repo.git(checkout, "checkout", "-q", "--detach", graph.installed)
    fixture_git.run(checkout, "commit", "--allow-empty", "-q", "-m",
                    "Merge pull request #41 from OpenHCSDev/fix/due", "-m", "Install what merged")
    merge = Repo.git(checkout, "rev-parse", "HEAD").strip()
    Repo.git(checkout, "push", "origin", f"{merge}:refs/heads/main")
    github_cli.configure(graph.repository.github)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    github_cli.put(graph.repository.github, UnmergedListedPullRequest(
        42, "fix/next", "b" * 40, stamp, "main", reviewable_times=(stamp,), armed_times=(stamp,)))
    bus = wire(tmp_path / "bus")
    bus.registry.declare(NativeThread("colleague", frozenset(), Checkout(checkout)))
    Team.on_root("colleague", (graph.repository,), bus.root)
    merged = int(Repo.git(checkout, "log", "-1", "--format=%ct", merge))
    monkeypatch.setattr(time, "time", lambda: merged + 60)

    pulls = ProgressRead()
    InstallDue.prepare(bus, HeldOrders.read(bus), pulls)
    (due,) = InstallDue.due(bus, pulls)
    assert due.main == merge
    assert "#41 Install what merged" in due.line() and "#42 fix/next" in due.line()
    assert "order install" in due.line().rsplit(" -> ", 1)[1]
    due.enforce(bus)
    assert InstallLogs(bus.root).units() == ()




def test_a_delivered_order_asks_the_build_whether_its_install_is_still_owed(
    proof_build_checkouts, comms, tmp_path,
):
    """P42 and P48 sat on Tristan's board reading "waiting on install" while their delivered
    commits were already in the running build. DeliveredProblemOrder.line spelled that phrase
    as a literal and inherited the default installed_line, which ignores the build, so the
    board asserted an install state nobody had asked about. Both halves already had owners:
    OrderDelivery.installation_commits knows which commits a delivery installs, and each
    CommitInstallation member owns the words for its answer. This is the merged order's
    control one member over, for the delivered member."""
    from test_todos import order

    graph = next(graph for graph in proof_build_checkouts
                 if graph.repository.github == CorePackage.repository)
    delivery = CheckedCommitDelivery(
        commit=graph.newer, delivered_commit=graph.newer,
        merge=MainDeliveryMerge(), at=RecordedEventTime(1000),
    )
    delivered = order(graph.repository).delivered(delivery)
    assert delivery.installation_commits == (graph.newer,)

    # Says what was delivered and claims nothing it has no build to know.
    assert "install" not in delivered.line

    assert delivered.installed_line(RecordedBuildIdentity.installed()).endswith(
        "waiting on install")

    resolution = RuntimeResolution.at(LaunchRuntime.installed().prefix)
    resolution.model_copy(update={"core_commit": graph.newer}).save(
        LaunchRuntime.installed().prefix)

    assert delivered.installed_line(RecordedBuildIdentity.installed()).endswith("installed")


def test_only_an_order_still_owed_an_install_lets_the_build_speak(
    proof_build_checkouts, comms, tmp_path,
):
    """Asking the build is right for an order awaiting one and wrong for every state that
    owes none. A returned triage answer would have read "Report returned, installed", and a
    completed Installed state would have turned into "Installed, waiting on install" the
    moment a later runtime stopped containing the commit whose run had already been
    witnessed. The second one was reachable before the delivered line ever asked the build,
    through the merge-observed installed order.

    Each member already says what it owes, in its own line and in waiting_on_install; this
    is that same answer where the board reads it. InstalledEvidence sits first in the MRO of
    both installed members, so one declaration covers them.
    """
    from test_todos import order

    graph = next(graph for graph in proof_build_checkouts
                 if graph.repository.github == CorePackage.repository)
    build = RecordedBuildIdentity.installed()
    checked = order(graph.repository)

    # The triage answer's own path, todos.py:2588, not `delivered`, which always builds the
    # plain delivered member whatever kind of delivery it is handed.
    reported = checked.copy_as(
        ReportedProblemOrder,
        delivery=ReportDelivery(report="the findings", at=RecordedEventTime(1000)))
    assert reported.installed_line(build) == "Report returned"

    works, artifact = "the application ran", str(tmp_path / "proof.txt")
    installed_merge = checked.merged(graph.newer).installed(works, artifact)
    assert installed_merge.installed_line(build) == "Installed"

    delivery_commits = CheckedCommitDelivery(
        commit=graph.newer, delivered_commit=graph.newer,
        merge=MainDeliveryMerge(), at=RecordedEventTime(1000),
    )
    delivered = checked.delivered(delivery_commits)
    assert delivered.installed_line(build).endswith("waiting on install")
    assert delivered.installation_commits == delivery_commits.installation_commits

    # Both installed members, not just the merge-observed one: InstalledEvidence is first in
    # the MRO of each, which is the whole reason one declaration can answer for both.
    assert delivered.installed(works, artifact).installed_line(build) == "Installed"
