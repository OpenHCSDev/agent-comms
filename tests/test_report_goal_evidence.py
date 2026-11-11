"""Report completion verifies original content before changing the real goal."""

import hashlib
from functools import partial

import pytest

from agent_comms.coordination.errors import Refused
from agent_comms.errors import GoalRefused
from agent_comms.execution.owner_protocol import OwnerFailure, OwnerLine
from agent_comms.execution.turn_context import FileProvenance
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.planning.goal_actions import SetGoalAction
from agent_comms.planning.goals import ProblemGoalOrder
from agent_comms.planning.progress_claims import ProgressClaim, ReportClaim
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tools.goals import CommsGoalTool


@pytest.fixture
def report_goal(comms, tmp_path):
    comms.registry.declare(NativeThread("editor", frozenset(), Checkout(tmp_path)))
    comms.goals.update_goal("editor", SetGoalAction(text="Deliver the consensus report"))
    return comms.registry.require("editor").goal.require()


ORIGINAL = b"# Consensus\r\nAll thirteen editorial findings are delivered.\r\n"


def provenance(path):
    return FileProvenance(str(path), hashlib.sha256(path.read_bytes()).hexdigest())


def claimed(source):
    return [{"kind": "report", "source": FieldCodec.encode(source)}]


def missing(path, source):
    path.unlink()
    return claimed(source)


def changed(path, source):
    path.write_text("Different report at the same path")
    return claimed(source)


def blank(path, source):
    path.write_text(" \n\t")
    return claimed(provenance(path))


def binary(path, source):
    path.write_bytes(b"\xff\xfe")
    return claimed(provenance(path))


def relative(path, source):
    return claimed(FileProvenance(path.name, source.sha256))


def unclaimed(path, source):
    return []


def complete(comms, goal, claims):
    CommsGoalTool.invoke(
        comms, dict(goal_id=goal.id, status="completed", progress="Report delivered", claims=claims),
        caller=ThreadCaller("editor"),
    )


@pytest.mark.parametrize("tamper", (missing, changed, blank, binary, relative, unclaimed),
                         ids=lambda tamper: tamper.__name__)
def test_report_completion_refuses_content_it_cannot_authenticate(comms, tmp_path, report_goal, tamper):
    path = tmp_path / "consensus.md"
    path.write_bytes(ORIGINAL)
    claims = tamper(path, provenance(path))
    with pytest.raises((ValueError, OSError)):
        complete(comms, report_goal, claims)
    assert comms.registry.require("editor").goal.require() == report_goal


def test_report_completion_authenticates_content_before_transition(comms, tmp_path, report_goal):
    path = tmp_path / "consensus.md"
    path.write_bytes(ORIGINAL)
    source = provenance(path)
    claim = FieldCodec.decode(ProgressClaim, claimed(source)[0])
    assert claim == ReportClaim(source)
    assert ProgressClaim.require_all((claim,), comms, tmp_path) == {claim: (source.public_description(),)}
    complete(comms, report_goal, claimed(source))
    completed = comms.registry.require("editor").goal.require()
    assert completed.id == report_goal.id
    assert completed.state.declared_name == "completed"
    assert completed.progress == "Report delivered"
    assert path.read_bytes() == ORIGINAL


def test_report_is_not_implementation_order_completion(comms, tmp_path, report_goal):
    path = tmp_path / "report.md"
    path.write_text("Delivered analysis, not an implemented fix")
    claim = ReportClaim(FileProvenance(str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
    ProgressClaim.require_all((claim,), comms, tmp_path)
    policy = ProblemGoalOrder()
    for operation in (
        partial(policy.require_completion, report_goal, comms.goals, comms.registry.require("editor").incarnation),
        policy.require_edit,
        policy.require_model_block,
    ):
        with pytest.raises(GoalRefused) as denied:
            operation()
        with pytest.raises(Refused) as reply:
            OwnerLine.read(OwnerFailure.refused(denied.value).wire()).reply()
        assert reply.value.code == denied.value.code == Refused.INVALID_PARAMS
        assert str(reply.value) == str(denied.value)
