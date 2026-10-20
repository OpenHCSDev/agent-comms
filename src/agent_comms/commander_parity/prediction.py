"""A retained forecast survives two real heartbeat reviews and native closing."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

from ..commander_heartbeat import CommanderReview
from ..field_codec import FieldCodec
from ..goals import CheckedCommit, OrderCheck
from ..storage_rules import StorageRule
from ..todos import FinishedPrediction, MovingTrend, ReviewedPrediction, Todo
from ..tool_result import ToolResult
from ..tools import CommsProblemCloseTool, CommsTodoListTool, TodoList
from .lifecycle import LifecycleCase, LifecycleRun


class PredictionCase(LifecycleCase):
    native_owner = CommsTodoListTool
    additional_owners = (CommsProblemCloseTool,)

    @staticmethod
    def git(checkout: Path, *arguments: str, **environment: str) -> str:
        return subprocess.check_output(
            ("git", "-C", str(checkout), *arguments),
            env=os.environ | environment,
            text=True,
            stderr=subprocess.PIPE,
            timeout=30,
        ).strip()

    @classmethod
    def record_check(cls, check: OrderCheck, checkout: Path) -> None:
        result = asyncio.run(check.run())
        CheckedCommit(check.command, result.outcome, result.stdout.decode()).store(
            str(checkout), cls.git(checkout, "rev-parse", "HEAD")
        )

    @classmethod
    def exercise(cls, run: LifecycleRun) -> tuple[Todo, ...]:
        created = run.create()
        comms = run.commander.comms
        root = comms.root
        # The copy owns its policy and local Git history, never the source worker's checkout.
        policy = replace(comms.registry.storage_rule, checkout_base=root / "projects")
        (root / "storage.json").write_text(json.dumps(FieldCodec.encode(policy)))
        # Registration captures configuration for its lifetime; load the copy's new policy.
        comms.registry.storage_rule = StorageRule.load(root)
        (root / "commander.json").write_text(run.commander.model_dump_json())
        checkout = policy.order_checkout(run.worker, created.id)
        checkout.mkdir(parents=True)
        cls.git(checkout, "init", "-q", "-b", "main")
        cls.git(checkout, "config", "user.name", "Prediction comparison")
        cls.git(checkout, "config", "user.email", "prediction@example.invalid")
        origin = root / "origin.git"
        subprocess.run(("git", "init", "-q", "--bare", str(origin)), check=True, timeout=30)
        cls.git(checkout, "remote", "add", "origin", str(origin))
        source = checkout / "decisions.txt"
        source.write_text("100")
        cls.git(checkout, "add", source.name)
        ordered = created.order.checked().at.recorded_at("The copied order")
        base_date = f"@{int(ordered - 1)} +0000"
        cls.git(
            checkout,
            "commit",
            "-qm",
            "Retained base",
            GIT_AUTHOR_DATE=base_date,
            GIT_COMMITTER_DATE=base_date,
        )
        cls.git(checkout, "push", "-q", "origin", "main")
        cls.git(checkout, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        check = created.order.checked().check
        run.decisions.write_text(json.dumps(["unfinished"] * 100))
        cls.record_check(check, checkout)
        cls.git(checkout, "switch", "-qc", "work")
        heartbeat = CommanderReview()
        commander = comms.registry.require(run.commander.commander)
        rows = [created]
        for offset, remaining in ((2, 60), (4, 20)):
            source.write_text(str(remaining))
            cls.git(checkout, "add", source.name)
            date = f"@{int(ordered + offset)} +0000"
            cls.git(
                checkout,
                "commit",
                "-qm",
                f"{remaining} decisions remain",
                GIT_AUTHOR_DATE=date,
                GIT_COMMITTER_DATE=date,
            )
            run.decisions.write_text(json.dumps(["unfinished"] * remaining))
            cls.record_check(check, checkout)
            heartbeat.wake(comms, run.worker, commander)
            rows.append(comms.goals.todos.get(created.id))
            # The current command and native owner must expose the exact retained forecast.
            run.commander.write_board()
            measurement = run.commander.invocations[-1].result
            if not isinstance(measurement, TodoList):
                raise ValueError("Board did not read its native TodoList")
            print(f"Review: {measurement.progress[created.id].line}")
        # The fixture repository is retained in the disposable copy, no longer an open checkout.
        checkout.rename(root / "completed-history")
        run.complete(rows[-1])
        run.commander.close(
            created.id,
            "Read the copied native goal and confirm its recorded source location "
            "and assigned worker.",
            str(run.artifact),
        )
        rows.append(comms.goals.todos.get(created.id))
        return tuple(rows)

    @classmethod
    def checks(
        cls, run: LifecycleRun, rows: tuple[Todo, ...], native: tuple[ToolResult, ...], output: str
    ) -> tuple[tuple[str, bool], ...]:
        reads = tuple(result for result in native if isinstance(result, TodoList))
        if len(rows) != 4 or len(reads) < 3:
            return (("two native reviews and closing observed", False),)
        pid = rows[0].id
        # Each board invocation carries the exact native result; compare no second live read.
        finishes = tuple(read.progress[pid].finish for read in reads if pid in read.progress)
        reviewed = tuple(finish for finish in finishes if isinstance(finish, ReviewedPrediction))
        finished = tuple(finish for finish in finishes if isinstance(finish, FinishedPrediction))
        if len(reviewed) < 2 or not finished:
            return (("reviewed and actual finish retained", False),)
        first, last, closed = reviewed[0], reviewed[-1], finished[-1]
        board = run.commander.board.read_text()
        messages = run.commander.comms.bus.inbox(run.commander.commander)
        return (
            (
                "both reviews derive numeric measured slopes",
                isinstance(first.trend, MovingTrend) and isinstance(last.trend, MovingTrend),
            ),
            ("second review replaces first", first != last and rows[1].revision < rows[2].revision),
            ("closure retains last prediction", closed.forecast == last),
            ("actual finish recorded", closed.actual >= last.at),
            (
                "native row actually closed",
                not rows[-1].state.open and pid in {t.id for t in reads[-1].closed},
            ),
            ("board shows predicted against actual", closed.line in board),
            ("comparison shows signed error", "actual minus predicted" in closed.line),
            (
                "heartbeat shows its saved forecast",
                any("predicted finish" in message.body for message in messages),
            ),
        )
