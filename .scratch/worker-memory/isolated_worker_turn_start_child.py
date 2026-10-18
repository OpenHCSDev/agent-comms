import asyncio, json, os, time
from pathlib import Path

from agent_comms.cursor_publication import CursorDelivery
from agent_comms.native_entries import NativeEvidenceScope
from agent_comms.worker import run

SESSION = Path(os.environ['MEMORY_FORK_SESSION'])
TIMINGS = Path(os.environ['MEMORY_TURN_TIMINGS'])

async def workload():
    delivery = CursorDelivery()
    retained = getattr(delivery, 'source_reads', None)
    try:
        with TIMINGS.open('w', buffering=1) as output:
            while True:
                begun = time.perf_counter_ns()
                with NativeEvidenceScope.borrow(retained) as source_reads:
                    _header, entries = source_reads.for_source(SESSION).observe()
                elapsed_ms = (time.perf_counter_ns() - begun) / 1_000_000
                output.write(json.dumps({
                    'elapsed_ms': elapsed_ms,
                    'entries': len(entries),
                    'verified_bytes': SESSION.stat().st_size,
                }) + '\n')
                await asyncio.sleep(5)
    finally:
        if retained is not None:
            retained.close()

async def main():
    task = asyncio.create_task(workload())
    try:
        await run()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

if __name__ == '__main__':
    asyncio.run(main())
