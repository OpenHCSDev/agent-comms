"""Order validation uses its declared repository, not the connector's cwd."""

import os
import subprocess
import sys
from pathlib import Path


def test_pending_problem_order_from_outside_repository(tmp_path):
    repository = tmp_path / "repository"
    source = repository / "src" / "owner.py"
    source.parent.mkdir(parents=True)
    source.write_text("owned = True\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    program = """
import sys, time
from pathlib import Path
from agent_comms.change_report import FileLine
from agent_comms.comms import Comms
from agent_comms.goal_actions import CreateProblemGoalAction, SetGoalAction
from agent_comms.goals import OrderCheck, RequestGoalOrder
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import MechanicalOrderRun, PendingProblemOrder
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsTodoCreateTool
root, repository = map(Path, sys.argv[1:])
root.mkdir()
comms = Comms(root)
for name in ('lead', 'worker'):
    comms.registry.declare(comms.registry.new_thread(name, frozenset({'comms'}), str(repository)))
comms.goals.update_goal('lead', SetGoalAction(
    text='Owner request', goal_id='request', order=RequestGoalOrder()))
now = time.time()
order = PendingProblemOrder(
    id='rooted-order', owner=FileLine('src/owner.py', 1),
    check=OrderCheck(str(repository), 'printf 1'), run=MechanicalOrderRun(),
    edits=('Repair the owning declaration',), banned=('No second store',),
    report='Report the result', at=RecordedEventTime(now),
    first_call_deadline=RecordedEventTime(now + 120))
CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
    problem='P1', order=order, request='request',
    deadline=RecordedEventTime(now + 3600), commander='lead',
    repo=RepositoryName.from_wire('OpenHCSDev/agent-comms'), text='Repair the source owner',
)).apply(comms, ThreadCaller('lead'))
assert comms.goals.todos.get('P1').order.owner == FileLine('src/owner.py', 1)
print('pending order validated outside repository')
"""
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "bus"), str(repository)],
        cwd=outside, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "pending order validated outside repository"
