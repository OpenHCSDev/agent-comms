"""Source-bound real launcher; its independent deadline outlives this process."""

import asyncio
import json
import sys
import time
from pathlib import Path

# Child launch deliberately drops ambient PYTHONPATH. Select this fixture's own
# checkout explicitly, rather than silently testing the shared Python's package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from agent_comms.child_process import NamespacedChild
from agent_comms.field_codec import FieldCodec


async def main() -> None:
    child = await NamespacedChild.start(
        (sys.executable, '-c', 'import time; time.sleep(60)'),
        deadline=time.monotonic() + 3,
    )
    Path(sys.argv[1]).write_text(json.dumps(FieldCodec.encode(child.namespace)))
    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())
