"""Run one arm on one task from the task's frozen snapshot.

The agent gets a fresh worktree at the snapshot, the goal text and a wall-clock
budget; nothing else (no evaluation harness, no prior agent's notes). After the
budget, the worktree's branch is scored with score.py.

Usage:
    python run_arm.py TASK_ID ARM_ID REPO_CHECKOUT [--hours 12] [--out runs/]
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import time
import tomllib
from pathlib import Path

PROMPT = """You are working in this repository. Goal:

{goal}

Commit your work as you go. You have {hours} hours of wall time."""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("arm")
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--hours", type=float, default=12)
    parser.add_argument("--out", type=Path, default=Path("runs"))
    args = parser.parse_args()
    spec = tomllib.loads((Path(__file__).parent / "tasks.toml").read_text())
    task = next(t for t in spec["task"] if t["id"] == args.task)
    arm = next(a for a in spec["arm"] if a["id"] == args.arm)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    branch = f"bench/{task['id']}/{arm['id']}/{stamp}"
    worktree = (args.out / f"{task['id']}--{arm['id']}--{stamp}").resolve()
    subprocess.run(["git", "-C", args.checkout, "worktree", "add", "-b", branch, worktree, task["snapshot"]], check=True)
    # The evaluation harness is not part of the snapshot the agent sees.
    subprocess.run(["rm", "-rf", worktree / "bench"], check=True)

    prompt = PROMPT.format(goal=task["goal"].strip(), hours=args.hours)
    log = worktree.with_suffix(".log")
    started = time.time()
    with log.open("w") as sink:
        try:
            subprocess.run(
                [*shlex.split(arm["command"]), prompt],
                cwd=worktree, stdout=sink, stderr=subprocess.STDOUT,
                timeout=args.hours * 3600,
            )
        except subprocess.TimeoutExpired:
            sink.write("\n[bench] budget exhausted\n")
    elapsed = time.time() - started
    print(f"{branch}\t{worktree}\t{elapsed / 3600:.2f} h\tlog {log}")


if __name__ == "__main__":
    main()
