"""Real Core board reads consume the recorded check run result, never run the check."""


import argparse
import shlex
import subprocess

from proofs.cn_phase3_record import CHECKOUT, ROOT, cli, create_order

from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import CheckedCommit, CountCheckResult, OrderCheck
from agent_comms.repo import Repo
from agent_comms.todos import CorrectCheckTodo, DistanceReadings, PushedProgress, Todo
from agent_comms.order_tools import CommsChangeProblemTool, CommsTodoListTool, TodoList

MARKER = ROOT / "explicit-check-count"
MARKER_CONTENT = b"x"


def prepare() -> None:
    create_order("P6")
    comms = wire(ROOT)
    todo = comms.goals.todos.get("P6")
    check = OrderCheck(
        str(CHECKOUT), f"printf x >> {shlex.quote(str(MARKER))}; printf 0", CountCheckResult(0)
    )
    cli(
        "record-lead",
        CommsChangeProblemTool(
            problem="P6",
            operation=CorrectCheckTodo(
                todo.revision,
                comms.registry.require("record-lead").incarnation,
                todo.order.checked().check,
                check,
                "Prove that reading the board cannot execute the check",
            ),
        ),
    )


def board(todo: Todo) -> DistanceReadings:
    listed = FieldCodec.decode(TodoList, FieldCodec.parse(cli("record-lead", CommsTodoListTool())))
    progress = listed.progress[todo.id]
    assert isinstance(progress, PushedProgress), progress
    return progress.distance_readings()


def verify() -> None:
    comms = wire(ROOT)
    todo = comms.goals.todos.get("P6")
    check = todo.order.checked().check
    checkout = comms.registry.storage_rule.order_checkout(todo.held().owner, todo.id)
    if checkout.exists() or MARKER.exists():
        raise RuntimeError("Proof resources already exist; inspect before another attempt")
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(checkout), "HEAD"],
        cwd=CHECKOUT,
        check=True,
        timeout=60,
    )
    try:
        assert not board(todo).readings
        assert not MARKER.exists()
        sha = Repo.git(checkout, "rev-parse", "HEAD").strip()
        assert not check.recorded(sha, str(checkout))
        check.at(sha, str(checkout), comms.registry.storage_rule.checkout_base)
        assert MARKER.read_bytes() == MARKER_CONTENT
        assert len(board(todo).readings) == 1
        assert MARKER.read_bytes() == MARKER_CONTENT
        for result in CheckedCommit.stored(str(checkout), sha):
            if result.command == check.command:
                print(FieldCodec.encode(result), flush=True)
        print(
            "Actual CLI: unknown before explicit run, recorded afterward; exactly one execution.",
            flush=True,
        )
    finally:
        subprocess.run(
            ["git", "worktree", "remove", str(checkout)], cwd=CHECKOUT, check=True, timeout=60
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    steps = parser.add_subparsers(required=True)
    steps.add_parser(prepare.__name__).set_defaults(action=prepare)
    steps.add_parser(verify.__name__).set_defaults(action=verify)
    args = parser.parse_args()
    args.action()
