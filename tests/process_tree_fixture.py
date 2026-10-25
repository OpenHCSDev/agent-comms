"""A real child and TERM-defiant grandchild, shared by process lifetime checks."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec

TREE = """import subprocess,sys,os,time,json
from pathlib import Path
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec
grandchild = ('import signal,time,sys; '
              'stop = signal.SIGBREAK if sys.platform=="win32" else signal.SIGTERM; '
              'signal.signal(stop, signal.SIG_IGN); print("ready",flush=True); time.sleep(60)')
child=subprocess.Popen([sys.executable,'-c',grandchild],stdout=subprocess.PIPE)
assert child.stdout.readline()==b'ready\\n'
identities=[FieldCodec.encode(ProcessIdentity.capture(p)) for p in (os.getpid(),child.pid)]
Path(sys.argv[1]).write_text(json.dumps(identities))
print('tree ready',flush=True)
time.sleep(60)
"""


def identities(path: Path) -> tuple[ProcessIdentity, ...]:
    return tuple(FieldCodec.decode(ProcessIdentity, row) for row in json.loads(path.read_text()))


async def ready(path: Path) -> None:
    """Until the tree announces itself; pytest-timeout owns the budget."""
    while not path.exists():
        await asyncio.sleep(0.01)
