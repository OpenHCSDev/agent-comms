"""Real Core board reads consume explicit check receipts, never run the check."""
import argparse
import shlex
import subprocess

from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import CheckedCommit, CountCheckResult, OrderCheck
from agent_comms.todos import CorrectCheckTodo, PushedProgress
from agent_comms.tools import CommsTodoListTool, CommsTodoTool, TodoList
from proofs.cn_phase3_record import CHECKOUT, ROOT, cli, create_order

MARKER = ROOT / 'explicit-check-count'
MARKER_CONTENT = b'x'


def prepare():
    create_order('P6')
    comms = Comms(ROOT)
    todo = comms.goals.todos.get('P6')
    check = OrderCheck(str(CHECKOUT), f'printf x >> {shlex.quote(str(MARKER))}; printf 0',
                       CountCheckResult(0))
    cli('record-lead', CommsTodoTool(id='P6', operation=CorrectCheckTodo(
        todo.revision, comms.registry.require('record-lead').incarnation,
        todo.order.check, check, 'Prove that reading the board cannot execute the check')))


def board():
    listed = FieldCodec.decode(TodoList, FieldCodec.parse(cli('record-lead', CommsTodoListTool())))
    progress = listed.progress['P6']
    assert isinstance(progress, PushedProgress), progress
    return progress.distance_readings()


def verify():
    comms = Comms(ROOT)
    todo = comms.goals.todos.get('P6')
    checkout = comms.registry.storage_rule.order_checkout(todo.held().owner, todo.id)
    if checkout.exists() or MARKER.exists():
        raise RuntimeError('Proof resources already exist; inspect before another attempt')
    subprocess.run(['git', 'worktree', 'add', '--detach', str(checkout), 'HEAD'],
                   cwd=CHECKOUT, check=True, timeout=60)
    try:
        assert not board().readings
        assert not MARKER.exists()
        sha = CheckedCommit.git(str(checkout), 'rev-parse', 'HEAD').strip()
        assert not todo.order.check.recorded(sha, str(checkout))
        todo.order.check.at(sha, str(checkout))  # Deliberately explicit, never a board read.
        assert MARKER.read_bytes() == MARKER_CONTENT
        assert len(board().readings) == 1
        assert MARKER.read_bytes() == MARKER_CONTENT
        for receipt in CheckedCommit.stored(str(checkout), sha):
            if receipt.command == todo.order.check.command:
                print(FieldCodec.encode(receipt), flush=True)
        print('Actual CLI: unknown before explicit run, recorded afterward; exactly one execution.',
              flush=True)
    finally:
        subprocess.run(['git', 'worktree', 'remove', str(checkout)], cwd=CHECKOUT,
                       check=True, timeout=60)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    steps = parser.add_subparsers(required=True)
    steps.add_parser(prepare.__name__).set_defaults(action=prepare)
    steps.add_parser(verify.__name__).set_defaults(action=verify)
    args = parser.parse_args()
    args.action()
