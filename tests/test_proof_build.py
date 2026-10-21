"""Build-bound close uses real repositories and a retained-history bus copy."""

import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agent_comms.build_identity import (
    DeliveredCommitNotInstalledError,
    ProofBuildMismatchError,
    RecordedBuildIdentity,
    UninstalledRepositoryError,
)
from agent_comms.change_report import FileLine
from agent_comms.check_root import CheckRoot
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction, SetGoalAction
from agent_comms.goals import CountCheckResult, OrderCheck, RequestGoalOrder
from agent_comms.installer.runtime_resolution import (
    ArchiveRecord,
    CommitRecord,
    CorePackage,
    GitRecord,
    PackageRecord,
    RuntimePackage,
    RuntimeResolution,
)
from agent_comms.order_runs import ApplicationOrderRun
from agent_comms.progress_claims import WorkingClaim
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import (
    CommitDelivery,
    FileDeliveryEvidence,
    OperationDelivery,
    PendingProblemOrder,
    Todo,
)
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsOrderDoneTool, CommsProblemCloseTool, CommsTodoCreateTool
from agent_comms.work_branches import WorkBranches
from order_scope_fixture import FIX_SCOPE


@dataclass(frozen=True)
class ProofBuildCheckout:
    repository: Repo
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
        checkout = tmp_path / package.declared_name
        subprocess.run(("git", "init", str(checkout)), check=True, capture_output=True)
        subprocess.run(
            (
                "git",
                "remote",
                "add",
                "origin",
                f"https://github.com/{package.repository.to_wire()}.git",
            ),
            cwd=checkout,
            check=True,
            capture_output=True,
        )
        commits = []
        for number in range(3):
            subprocess.run(
                (
                    "git",
                    "-c",
                    "user.name=Proof Build",
                    "-c",
                    "user.email=proof@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
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
        graph = ProofBuildCheckout(Repo.of(checkout), *commits)
        checkouts.append(graph)
        subprocess.run(
            ("git", "update-ref", f"refs/remotes/origin/{WorkBranches.main}", graph.newer),
            cwd=checkout,
            check=True,
            capture_output=True,
        )
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
    source = Comms(tmp_path / "source")
    source.messaging.initialize_private_initial_protocol()
    for number, name in enumerate(("lead", "worker"), 1):
        source.registry.declare(
            source.registry.new_thread(
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
        copied.goals.update_goal(
            "lead",
            SetGoalAction(
                text="Record the copied application proof",
                goal_id="request",
                order=RequestGoalOrder(),
            ),
        )
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
                owner=FileLine(__file__, 1),
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
                        request="request",
                        review_at=RecordedEventTime(4102444800),
                        commander="lead",
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
    with pytest.raises(subprocess.CalledProcessError):
        RecordedBuildIdentity.installed().contains("0" * 40, graph.repository.checkout)
