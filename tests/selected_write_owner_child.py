"""Disposable provider-free worker for public ACP selected-write integration test."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from agent_comms import acp, cohort_foreground, coordinated_runtime, worker
from test_coordinated_runtime import _fake_model


async def main(base: Path) -> None:
    ready = base / "registered"
    for _ in range(600):
        if ready.exists():
            break
        await asyncio.sleep(0.01)
    else:
        raise RuntimeError("parent did not register child")
    cohort_foreground._trusted_package = lambda _: None
    coordinated_runtime._trusted_package = lambda _: None
    fake, calls = _fake_model(decision="FULL")
    coordinated_runtime.run_native_pi_turn = fake
    original_load = acp.CommsAgent.load_session
    acp.CommsAgent._ensure_live_drain = lambda self, _: None  # fixture-only dispatch hold

    async def loaded(self, *args, **kwargs):
        result = await original_load(self, *args, **kwargs)
        print(json.dumps({"event": "ready", "pid": os.getpid()}), flush=True)

        async def once() -> None:
            go = base / "dispatch"
            for _ in range(1200):
                if go.exists():
                    break
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError("parent did not release fixture dispatch")
            count = await self._drain_private_nk(
                "beta", os.environ["AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID"]
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
        return result

    acp.CommsAgent.load_session = loaded
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
