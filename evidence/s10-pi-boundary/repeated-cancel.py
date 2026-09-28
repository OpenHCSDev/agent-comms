import asyncio
import sys
from agent_comms.child_process import BoundedRun

async def main():
    ready = asyncio.Event()
    launched = []
    async def operation():
        async with BoundedRun.session((sys.executable, '-u', '-c',
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"), timeout=20) as child:
            launched.append(child)
            assert await child.stdout.readline() == b'ready\n'
            ready.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(operation())
    await ready.wait()
    task.cancel()
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    child = launched[0]
    print(f'caller finished while child alive: {child.alive()}', flush=True)
    await child.stop()
    print(f'after retained cleanup child alive: {child.alive()}', flush=True)
asyncio.run(main())
