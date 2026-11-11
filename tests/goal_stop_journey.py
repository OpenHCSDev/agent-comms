"""Real install-style stop on a copied private bus with a controlled provider."""


import asyncio
import shutil
from contextlib import closing
from pathlib import Path

from agent_comms.cli_verbs.stop import StopCliCommand
from agent_comms.comms import wire
from agent_comms.execution.native_arguments import GivenArguments
from agent_comms.installer.native_package import VerifiedNativePackage
from agent_comms.planning.goal_actions import SetGoalAction
from agent_comms.planning.goal_attempt_phase import FailedAttempt, SucceededAttempt
from agent_comms.planning.goal_generation import ReadyGeneration
from agent_comms.planning.goal_ledger import AttemptRecord, FailedTurnEvidence
from agent_comms.planning.goal_states import ActiveGoal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import NoObservedTurn
from agent_comms.registry.threads import NativeThread
from native_backend_fixture import native_backend_fixture, wait_for
from source_programs import SourcePrograms


async def stopped_goal_journey(
    base: Path, package: VerifiedNativePackage, programs: SourcePrograms,
):
    base.mkdir(mode=0o700)
    async with native_backend_fixture(base, package=package) as fixture:
        programs.bind(fixture.project)
        fixture.provider.response_gate = asyncio.Event()
        copied = base / "copy"
        shutil.copytree(fixture.root, copied)
        comms = wire(copied)
        comms.registry.declare(NativeThread(
            "stop-copy", frozenset(), Checkout(fixture.project)))
        comms.goals.update_goal("stop-copy", SetGoalAction(
            text="Continue the synthetic stopped-goal journey."))
        # The real lifecycle registers the private participant before launch.
        # Running worker directly would bypass that required source owner.
        await asyncio.to_thread(comms.owners.start_many, ("stop-copy",),
                                agent_args=GivenArguments(fixture.native_arguments()))
        try:
            await wait_for(lambda: fixture.provider.posts > 0)
            store = comms.goals.open_goal_store()
            original = store.snapshot(comms.registry.require("stop-copy").goal.require().id)
            assert original is not None
            await asyncio.to_thread(StopCliCommand(name="stop-copy").apply, comms)
            stopped = comms.registry.require("stop-copy")
            assert isinstance(stopped.goal.require().state, ActiveGoal), stopped.goal
            assert not stopped.process_alive
            ready = store.snapshot(stopped.goal.require().id)
            assert ready is not None and isinstance(ready.lifecycle, ReadyGeneration)
            assert ready.number == original.number + 1
            with closing(store._connect()) as db:
                attempt = AttemptRecord.one(db, attempt_id=original.attempt_id)
            assert attempt is not None and isinstance(attempt.phase, FailedAttempt)
            # A fresh continuation, not a resend of the interrupted input.
            await asyncio.to_thread(comms.owners.start_many, ("stop-copy",),
                                    agent_args=GivenArguments(fixture.native_arguments()))
            await wait_for(lambda: fixture.provider.posts >= 2)
            resumed = store.snapshot(stopped.goal.require().id)
            assert resumed is not None and resumed.number == ready.number
            fixture.provider.response_gate.set()
            def observed_turn():
                snapshot = comms.registry.snapshot()
                thread = snapshot.require("stop-copy")
                return thread, thread.observed_turn(snapshot.claim_generation("stop-copy"))

            _, stopped_turn = observed_turn()

            def continued_turn_finished():
                thread, observed = observed_turn()
                return (
                    not thread.executing
                    and observed != NoObservedTurn()
                    and observed != stopped_turn
                )

            await wait_for(continued_turn_finished)
            continued, continued_turn = observed_turn()
            assert continued_turn != NoObservedTurn()
            with closing(store._connect()) as db:
                finished = AttemptRecord.one(db, attempt_id=resumed.attempt_id)
            assert finished is not None and isinstance(finished.phase, SucceededAttempt)
            assert finished.progress_witness
            assert isinstance(continued.goal.require().state, ActiveGoal)
            assert continued.goal.require().id == stopped.goal.require().id
            await asyncio.to_thread(StopCliCommand(name="stop-copy").apply, comms)
            with closing(comms.goals.open_goal_store()._connect()) as db:
                failures = FailedTurnEvidence.count(db)
            return {
                "provider_posts": fixture.provider.posts,
                "stopped_turn": stopped_turn,
                "continued_turn": continued_turn,
                "failed_turn_observations": failures,
                "child_exited": not comms.registry.require("stop-copy").process_alive,
            }
        finally:
            await asyncio.to_thread(StopCliCommand(name="stop-copy").apply, comms)
