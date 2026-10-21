"""Build-bound close uses real native tools and a retained-history bus copy."""

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.change_report import FileLine
from agent_comms.check_root import CheckRoot
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction, SetGoalAction
from agent_comms.goals import CountCheckResult, OrderCheck, RequestGoalOrder
from agent_comms.launch_runtime import (
    DeliveredCommitNotInstalledError,
    ProofBuildMismatchError,
    RecordedBuildIdentity,
)
from agent_comms.progress_claims import WorkingClaim
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsOrderDoneTool, CommsProblemCloseTool, CommsTodoCreateTool
from agent_comms.todos import (
    CommitDelivery,
    FileDeliveryEvidence,
    OperationDelivery,
    PendingProblemOrder,
    Todo,
)
from agent_comms.order_runs import ApplicationOrderRun


def test_installed_proof_closes_and_older_build_is_refused_on_copied_bus(tmp_path):
    source = Comms(tmp_path / "source")
    source.messaging.initialize_private_initial_protocol()
    for number, name in enumerate(("lead", "worker"), 1):
        source.registry.declare(
            source.registry.new_thread(
                name, frozenset({"comms"}), str(tmp_path), created_at=float(number)
            )
        )
    installed = RecordedBuildIdentity.installed()
    older_core = subprocess.check_output(
        ("git", "rev-parse", f"{installed.core}^"),
        cwd=Path(__file__).resolve().parents[1],
        text=True,
    ).strip()
    older = replace(installed, core=older_core)
    checkout = Path(__file__).resolve().parents[1]
    checked = subprocess.check_output(
        ("git", "rev-parse", "origin/main"),
        cwd=checkout,
        text=True,
    ).strip()
    contained = subprocess.check_output(
        ("git", "rev-list", "--no-merges", "--max-count=1", installed.core),
        cwd=checkout,
        text=True,
    ).strip()
    missing = subprocess.check_output(
        ("git", "rev-list", "--no-merges", "--max-count=1", checked, "--not", installed.core),
        cwd=checkout,
        text=True,
    ).strip()
    assert contained and missing, (
        "The real checkout must have commits inside and outside this install"
    )
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
                        repo=RepositoryName.from_wire("OpenHCSDev/agent-comms"),
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
                revisions = (
                    (installed.core, installed.toad, older.core, older.toad)
                    if expected_error is ProofBuildMismatchError
                    else (missing, installed.core)
                )
                assert all(revision in str(refusal.value) for revision in revisions)
                assert copied.goals.todos.get(problem) == delivered
                print(f"{type(refusal.value).__name__}: {refusal.value}")
    assert not source.goals.todos.for_commander(source.registry.require("lead").incarnation)


def test_build_containment_does_not_turn_git_failure_into_absence_or_success():
    installed = RecordedBuildIdentity.installed()
    checkout = Path(__file__).resolve().parents[1]
    with pytest.raises(subprocess.CalledProcessError):
        installed.contains("0" * 40, checkout)
