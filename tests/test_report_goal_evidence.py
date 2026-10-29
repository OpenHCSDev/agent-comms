"""Report completion verifies original content before changing the real goal."""

import hashlib

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import ProblemGoalOrder
from agent_comms.progress_claims import ProgressClaim, ReportClaim
from agent_comms.repo import Checkout
from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsGoalTool
from agent_comms.turn_context import FileProvenance


@pytest.fixture
def report_goal(comms, tmp_path):
    comms.registry.declare(Thread("editor", frozenset(), Checkout(tmp_path)))
    comms.goals.update_goal("editor", SetGoalAction(text="Deliver the consensus report"))
    return comms.registry.require("editor").goal.require()


@pytest.mark.parametrize("failure", (None, "missing", "changed", "blank", "binary", "relative", "no-claim"))
def test_report_completion_authenticates_content_before_transition(comms, tmp_path, report_goal, failure):
    path = tmp_path / "consensus.md"
    original = b"# Consensus\r\nAll thirteen editorial findings are delivered.\r\n"
    path.write_bytes(original)
    source = FileProvenance(str(path), hashlib.sha256(original).hexdigest())
    if failure == "missing":
        path.unlink()
    elif failure == "changed":
        path.write_text("Different report at the same path")
    elif failure == "blank":
        path.write_text(" \n\t")
        source = FileProvenance(str(path), hashlib.sha256(path.read_bytes()).hexdigest())
    elif failure == "binary":
        path.write_bytes(b"\xff\xfe")
        source = FileProvenance(str(path), hashlib.sha256(path.read_bytes()).hexdigest())
    elif failure == "relative":
        source = FileProvenance(path.name, source.sha256)
    claims = [] if failure == "no-claim" else [
        {"kind": "report", "source": FieldCodec.encode(source)}
    ]
    arguments = dict(goal_id=report_goal.id, status="completed", progress="Report delivered", claims=claims)
    if failure:
        with pytest.raises((ValueError, OSError)):
            CommsGoalTool.invoke(comms, arguments, caller=ThreadCaller("editor"))
        assert comms.registry.require("editor").goal.require() == report_goal
    else:
        claim = FieldCodec.decode(ProgressClaim, claims[0])
        assert claim == ReportClaim(source)
        assert ProgressClaim.require_all((claim,), comms, tmp_path) == {
            claim: (source.public_description(),)
        }
        CommsGoalTool.invoke(comms, arguments, caller=ThreadCaller("editor"))
        completed = comms.registry.require("editor").goal.require()
        assert completed.id == report_goal.id
        assert completed.state.declared_name == "completed"
        assert completed.progress == "Report delivered"
        assert path.read_bytes() == original


def test_report_is_not_implementation_order_completion(comms, tmp_path, report_goal):
    path = tmp_path / "report.md"
    path.write_text("Delivered analysis, not an implemented fix")
    claim = ReportClaim(FileProvenance(str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
    ProgressClaim.require_all((claim,), comms, tmp_path)
    with pytest.raises(ValueError, match="checked delivered commit"):
        ProblemGoalOrder().require_completion(report_goal, comms.goals, comms.registry.require("editor").incarnation)
