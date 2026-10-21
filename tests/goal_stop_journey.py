"""Real install-style stop on a copied private bus with a controlled provider."""

import asyncio
import json
from contextlib import closing
import shutil
from pathlib import Path

from agent_comms.cli_commands import StopCliCommand
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goal_states import ActiveGoal
from agent_comms.goal_failure_observation import FailedTurnEvidence
from agent_comms.native_package import VerifiedNativePackage
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
        comms = Comms(copied)
        comms.registry.declare(comms.registry.new_thread(
            "stop-copy", frozenset(), str(fixture.project)))
        comms.goals.update_goal("stop-copy", SetGoalAction(
            text="Continue the synthetic stopped-goal journey."))
        # The real lifecycle registers the private participant before launch.
        # Running worker directly would bypass that required source owner.
        await asyncio.to_thread(comms.owners.start, "stop-copy",
                                agent_args=fixture.native_arguments())
        try:
            await wait_for(lambda: fixture.provider.posts > 0)
            before = FieldCodec.encode(comms.registry.require("stop-copy").goal)
            await asyncio.to_thread(StopCliCommand(name="stop-copy").apply, comms)
            stopped = comms.registry.require("stop-copy")
            after = FieldCodec.encode(stopped.goal)
            assert isinstance(stopped.goal.require().state, ActiveGoal), after
            assert not stopped.process_alive
            # A fresh continuation, not a resend of the interrupted input.
            await asyncio.to_thread(comms.owners.start, "stop-copy",
                                    agent_args=fixture.native_arguments())
            await wait_for(lambda: fixture.provider.posts >= 2)
            fixture.provider.response_gate.set()
            await wait_for(lambda: comms.registry.require("stop-copy").last_finished_turn_id
                           != stopped.last_finished_turn_id)
            continued = comms.registry.require("stop-copy")
            assert isinstance(continued.goal.require().state, ActiveGoal)
            assert continued.goal.require().id == stopped.goal.require().id
            await asyncio.to_thread(StopCliCommand(name="stop-copy").apply, comms)
            with closing(comms.goals.open_goal_store()._connect()) as db:
                failures = FailedTurnEvidence.count(db)
            result = {
                "before": before, "after": after,
                "continued": FieldCodec.encode(continued.goal),
                "provider_posts": fixture.provider.posts,
                "stopped_turn": stopped.last_finished_turn_id,
                "continued_turn": continued.last_finished_turn_id,
                "failed_turn_observations": failures,
                "child_exited": not comms.registry.require("stop-copy").process_alive,
                "root": str(copied),
            }
            print(json.dumps(result), flush=True)
            return result
        finally:
            await asyncio.to_thread(StopCliCommand(name="stop-copy").apply, comms)
