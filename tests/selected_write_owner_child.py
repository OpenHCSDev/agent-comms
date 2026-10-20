"""Disposable provider-free worker for public ACP selected-write integration test."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from agent_comms.tracked_turn import TrackedTurnSession
import agent_comms.native_pi as native_pi
import agent_comms.private_nk_entrypoint as private_nk_entrypoint
import agent_comms.worker as worker
from agent_comms.input_drain import InputDrain
from agent_comms.session_lifecycle import SessionLifecycle
from test_coordinated_runtime import _fake_model


async def main(base: Path) -> None:
    from agent_comms import native_package

    native_package.MANIFEST = Path(os.environ["AC_TEST_NATIVE_MANIFEST"]).resolve(strict=True)
    ready = base / "registered"
    for _ in range(600):
        if ready.exists():
            break
        await asyncio.sleep(0.01)
    else:
        raise RuntimeError("parent did not register child")
    fake, calls = _fake_model(decision="FULL")
    TrackedTurnSession.execute = fake
    original_start = SessionLifecycle.start_owner
    InputDrain.ensure_live_drain = lambda self, _: None  # fixture-only dispatch hold

    async def started(self, *args, **kwargs):
        await original_start(self, *args, **kwargs)
        agent = self.effects
        print(json.dumps({"event": "ready", "pid": os.getpid()}), flush=True)

        async def once() -> None:
            go = base / "dispatch"
            for _ in range(1200):
                if go.exists():
                    break
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError("parent did not release fixture dispatch")
            count = await agent._drain_private_nk(
                "beta", agent._comms.owners.require_private_launch().wire_root_id
            )
            print(
                json.dumps(
                    {
                        "event": "terminal",
                        "pid": os.getpid(),
                        "drained": count,
                        "fake_inputs": len(calls),
                    }
                ),
                flush=True,
            )

        asyncio.create_task(once())

    SessionLifecycle.start_owner = started
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
