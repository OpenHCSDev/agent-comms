"""Print the ``uv lock`` arguments that refresh a lock's branch-tracking packages, so a CI sync
refreshes our own siblings exactly as the installer's resolve step does:

    uv lock --project stack $(python -I -m agent_comms.installer.refresh_arguments stack/uv.lock)
"""

import sys
from pathlib import Path

from agent_comms.installer.runtime_resolution import ResolvedLock

if __name__ == "__main__":
    print(" ".join(ResolvedLock.read(Path(sys.argv[1])).refresh_arguments()))
