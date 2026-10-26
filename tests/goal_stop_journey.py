"""Real install-style stop on a copied private bus with a controlled provider."""


from agent_comms.repo import Checkout
import asyncio
import json
from contextlib import closing
import shutil
from pathlib import Path

from agent_comms.cli_commands import StopCliCommand
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goal_states import ActiveGoal
from agent_comms.goal_attempts import AttemptRecord
from agent_comms.goal_attempt_phase import FailedAttempt, SucceededAttempt
from agent_comms.goal_generation import ReadyGeneration
from agent_comms.goal_failure_observation import FailedTurnEvidence
from agent_comms.native_package import VerifiedNativePackage
from agent_comms.native_arguments import GivenArguments
from native_backend_fixture import native_backend_fixture, wait_for
from source_programs import SourcePrograms
from agent_comms.thread_identity import NoObservedTurn
from agent_comms.threads import Thread


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
        comms.registry.declare(Thread(
            "stop-copy", frozenset(), Checkout(fixture.project)))
        comms.goals.update_goal("stop-copy", SetGoalAction(
            text="Continue the synthetic stopped-goal journey."))
        # The real lifecycle registers the private participant before launch.
        # Running worker directly would bypass that required source owner.
        await asyncio.to_thread(comms.owners.start, "stop-copy",
                                agent_args=GivenArguments(fixture.native_arguments()))
        try:
            await wait_for(lambda: fixture.provider.posts > 0)
            before = FieldCodec.encode(comms.registry.require("stop-copy").goal)
            store = comms.goals.open_goal_store()
            original = store.snapshot(comms.registry.require("stop-copy").goal.require().id)
            assert original is not None
            await asyncio.to_thread(StopCliCommand(name="stop-copy").apply, comms)
            stopped = comms.registry.require("stop-copy")
            after = FieldCodec.encode(stopped.goal)
            assert isinstance(stopped.goal.require().state, ActiveGoal), after
            assert not stopped.process_alive
            ready = store.snapshot(stopped.goal.require().id)
            assert ready is not None and isinstance(ready.lifecycle, ReadyGeneration)
            assert ready.number == original.number + 1
            with closing(store._connect()) as db:
                attempt = AttemptRecord.one(db, attempt_id=original.attempt_id)
            assert attempt is not None and isinstance(attempt.phase, FailedAttempt)
            # A fresh continuation, not a resend of the interrupted input.
            await asyncio.to_thread(comms.owners.start, "stop-copy",
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
            result = {
                "before": before, "after": after,
                "continued": FieldCodec.encode(continued.goal),
                "provider_posts": fixture.provider.posts,
                "stopped_turn": FieldCodec.encode(stopped_turn),
                "continued_turn": FieldCodec.encode(continued_turn),
                "failed_turn_observations": failures,
                "stopped_attempt_phase": FieldCodec.encode(attempt.phase),
                "continued_attempt_phase": FieldCodec.encode(finished.phase),
                "continued_progress_witness": finished.progress_witness,
                "ready_generation": FieldCodec.encode(ready),
                "child_exited": not comms.registry.require("stop-copy").process_alive,
                "root": str(copied),
            }
            print(json.dumps(result), flush=True)
            (base / "stop-result.json").write_text(json.dumps(result, indent=2))
            return result
        finally:
            await asyncio.to_thread(StopCliCommand(name="stop-copy").apply, comms)
