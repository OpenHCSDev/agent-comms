"""Read-only actual native history; all derived files stay in an owned stage."""

import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path
from time import monotonic

from agent_comms.view_unread import TranscriptReadState

root = Path(os.environ["READ_INDEX_STAGE"])
root.mkdir(parents=True, exist_ok=True)
source = Path(
    "/home/ts/.pi/agent/sessions/--home-ts-code-projects-openhcs--/2026-09-22T03-50-07-145Z_01a0c73c-3628-7073-880c-e0df5f56e98e.jsonl"
)
state = TranscriptReadState(root / "read_ledger.json")
sources = {"agent-comms-ux": str(source)}
start = monotonic()
latencies = []
for _tick in range(1000):
    began = monotonic()
    result = state.counts("acceptance", sources)
    latencies.append(monotonic() - began)
    if not result.pending:
        break
    assert monotonic() - start < 25, "retained indexing did not converge"
assert not result.pending
assert result.counts["agent-comms-ux"] > 0
state.close()
fresh = TranscriptReadState(root / "read_ledger.json")
began = monotonic()
assert fresh.counts("acceptance", sources) == result
reopen = monotonic() - began
fresh.close()
print(
    json.dumps(
        {
            "source_bytes": source.stat().st_size,
            "ticks": len(latencies),
            "max_tick_s": max(latencies),
            "total_s": sum(latencies),
            "reopen_s": reopen,
            "counts": result.counts,
        }
    ),
    flush=True,
)


async def shutdown():
    cold = TranscriptReadState(root / "cancel" / "read_ledger.json")
    task = asyncio.create_task(asyncio.to_thread(cold.counts, "acceptance", sources))
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.to_thread(cold.close)
    with suppress(asyncio.CancelledError):
        await task


began = monotonic()
asyncio.run(shutdown())
print(json.dumps({"cancel_and_interpreter_executor_join_s": monotonic() - began}), flush=True)
print("PASS: interpreter shutdown completed", flush=True)
