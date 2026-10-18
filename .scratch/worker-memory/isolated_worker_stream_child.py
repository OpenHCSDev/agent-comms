import asyncio, json, os, time
from pathlib import Path

from agent_comms.native_entries import NativeEvidenceScope
from agent_comms.worker import run

SESSION = Path(os.environ['MEMORY_FORK_SESSION'])
SUMMARY = Path(os.environ['MEMORY_STREAM_SUMMARY'])
DURATION = int(os.environ.get('MEMORY_STREAM_SECONDS', '30'))
OBSERVATIONS_PER_APPEND = 3

async def workload():
    scope = NativeEvidenceScope()
    reader = scope.for_source(SESSION)
    begun_wall = time.perf_counter()
    begun_cpu = time.process_time()
    _header, entries = reader.observe()
    initial_done = time.perf_counter()
    appended = 0
    observations = 1
    try:
        for index in range(DURATION):
            target = initial_done + index
            await asyncio.sleep(max(0.0, target - time.perf_counter()))
            row = (json.dumps({
                'type': 'custom', 'id': f'controlled-stream-{index}',
                'customType': 'isolated-stream', 'data': {},
            }, separators=(',', ':')) + '\n').encode()
            with SESSION.open('ab', buffering=0) as output:
                output.write(row)
            appended += len(row)
            for _ in range(OBSERVATIONS_PER_APPEND):
                _header, entries = reader.observe()
                observations += 1
        result = {
            'duration_seconds': DURATION,
            'initial_wall_seconds': initial_done - begun_wall,
            'workload_wall_seconds': time.perf_counter() - begun_wall,
            'workload_cpu_seconds': time.process_time() - begun_cpu,
            'appended_bytes': appended,
            'appends': DURATION,
            'observations': observations,
            'entries': len(entries),
            'final_bytes': SESSION.stat().st_size,
        }
        SUMMARY.write_text(json.dumps(result, indent=2) + '\n')
    finally:
        scope.close()

async def main():
    worker = asyncio.create_task(run())
    try:
        await workload()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

if __name__ == '__main__':
    asyncio.run(main())
