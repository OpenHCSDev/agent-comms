"""Real install-style stop on a copied private bus with a controlled provider."""

import asyncio
import json
import os
import shutil
from pathlib import Path

from agent_comms.cli_commands import StopCliCommand
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import SetGoalAction
from agent_comms.native_package import VerifiedNativePackage
from native_backend_fixture import native_backend_fixture, wait_for


async def stopped_goal_journey(base: Path, package: VerifiedNativePackage):
    base.mkdir(mode=0o700)
    async with native_backend_fixture(base, package=package) as fixture:
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
        previous_path = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        try:
            await asyncio.to_thread(comms.owners.start, "stop-copy",
                                    agent_args=fixture.native_arguments())
            try:
                await wait_for(lambda: fixture.provider.posts > 0)
                before = FieldCodec.encode(comms.registry.require("stop-copy").goal)
                await asyncio.to_thread(StopCliCommand(name="stop-copy").apply, comms)
                result = {
                    "before": before,
                    "after": FieldCodec.encode(comms.registry.require("stop-copy").goal),
                    "provider_posts": fixture.provider.posts,
                    "child_exited": not comms.registry.require("stop-copy").process_alive,
                    "root": str(copied),
                }
                print(json.dumps(result), flush=True)
                return result
            finally:
                await asyncio.to_thread(StopCliCommand(name="stop-copy").apply, comms)
        finally:
            os.environ["PYTHONPATH"] = previous_path
