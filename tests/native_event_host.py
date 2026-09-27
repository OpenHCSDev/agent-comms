"""Opt-in, isolated SDK host of an already verified native test bundle."""

import asyncio
import sys
from pathlib import Path

from agent_comms.native_session_reopen import package_for_launcher


def install_event_host(monkeypatch, launcher, origin, *, delay_settlement=None, comms_tools=False):
    package = package_for_launcher(launcher)
    host = Path(__file__).with_suffix(".mjs")
    spawn = asyncio.create_subprocess_exec

    async def isolated_spawn(program, *args, **kwargs):
        if program == launcher:
            env = dict(kwargs["env"])
            for name in ("NODE_OPTIONS", "NODE_PATH", "NODE_COMPILE_CACHE"):
                env.pop(name, None)
            env.update(
                S1_NATIVE_PACKAGE=str(package), S1_LOCAL_ORIGIN=origin, S1_PYTHON=sys.executable
            )
            if delay_settlement is not None:
                env["S1_DELAY_SETTLEMENT"] = str(delay_settlement)
            if comms_tools:
                env["S1_COMMS_TOOLS"] = "1"
            kwargs["env"] = env
            return await spawn("node", str(host), *args, **kwargs)
        return await spawn(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", isolated_spawn)
