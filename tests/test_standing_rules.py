import json
import shutil
import subprocess

from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.plan_phases import RecordPlanePhase, ReviewQueuePhase, UnplannedPhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import DiskLow, PlanIdle, StandingRule
from agent_comms.storage_rules import UnitStorage
from agent_comms.todos import Todo
from agent_comms.tools import CommsOrderServeTool, CommsTodoCreateTool

from test_commander_problem_orders import create, order


def test_plan_idle_names_a_ready_phase_until_an_order_serving_it_is_open(commander):
    comms, caller = commander

    def idle():
        todos = comms.goals.todos.for_commander(comms.registry.require('lead').incarnation)
        return [rule.phase for rule in StandingRule.broken(todos, comms.registry) if isinstance(rule, PlanIdle)]

    assert RecordPlanePhase in idle() and ReviewQueuePhase not in idle()
    assert PlanIdle(RecordPlanePhase).line() == (
        'PLAN IDLE: phase record_plane ready, no open order -> dispatch it this wake')
    CommsTodoCreateTool(owner='worker', order=CreateProblemGoalAction(
        problem='P1', order=order(comms.root, 'order-P1'),
        request='request', deadline=RecordedEventTime(1000), commander='lead',
        repo='OpenHCSDev/agent-comms', text='Build the record plane',
    )).apply(comms, caller)
    assert comms.goals.todos.get('P1').order.serves is UnplannedPhase
    assert RecordPlanePhase in idle()
    CommsOrderServeTool(problem='P1', serves=RecordPlanePhase).apply(comms, caller)
    stored = comms.goals.todos.get('P1')
    assert stored.order.serves is RecordPlanePhase
    assert FieldCodec.decode(Todo, FieldCodec.encode(stored)) == stored
    assert RecordPlanePhase not in idle()


def test_storage_rules_report_each_threshold_on_a_scratch_root(commander, tmp_path):
    comms, _ = commander
    create(commander, 'P1')
    home, extra = tmp_path / 'wt' / 'worker', tmp_path / 'wt' / 'worker--P1'
    subprocess.run(['git', 'init', '-q', str(home)], check=True)
    subprocess.run(['git', '-C', str(home), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q',
                    '--allow-empty', '-m', 'base'], check=True)
    subprocess.run(['git', '-C', str(home), 'worktree', 'add', '-q', str(extra)], check=True)
    free = shutil.disk_usage(tmp_path).free / 2**30

    def broken(**limits):
        (comms.root / 'storage.json').write_text(json.dumps(
            {'volume': str(tmp_path), 'checkout_base': str(tmp_path / 'wt'), **limits}))
        read = Comms(comms.root)
        return StandingRule.broken(read.goals.todos.for_commander(read.registry.require('lead').incarnation),
                                   read.registry)

    def kinds(found):
        return {type(rule) for rule in found} - {PlanIdle}

    assert kinds(broken(cleanup_gib=0, floor_gib=0, max_checkouts=1)) == set()
    over, = [rule for rule in broken(cleanup_gib=0, floor_gib=0, max_checkouts=0) if type(rule) is UnitStorage]
    assert over.checkouts == (home.resolve(), extra.resolve())
    assert over.line() == ('VIOLATION storage quota: worker holds 1 checkouts besides home, limit 0'
                           ' -> order the cleanup')
    cleanup, = [rule for rule in broken(cleanup_gib=free + 100, floor_gib=0) if type(rule) is DiskLow]
    assert 'cleanup due under' in cleanup.line() and 'floor' not in cleanup.line()
    floor, = [rule for rule in broken(cleanup_gib=free + 100, floor_gib=free + 50) if type(rule) is DiskLow]
    assert floor.line().endswith('floor nothing allocates) -> put a cleanup owner on it now')
