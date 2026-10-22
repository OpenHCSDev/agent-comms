"""A real pool parent with an owned, TERM-defiant interpreter tree."""
import os
import sys
from pathlib import Path

from agent_comms.child_process import ForkedProcessPool, SpawnedProcess
from process_tree_fixture import TREE

if __name__ == "__main__":
    with ForkedProcessPool(2) as pool:
        pool.submit(SpawnedProcess.run, (sys.executable, "-c", TREE, sys.argv[1]),
                    timeout=60, cwd=Path.cwd(), env=dict(os.environ)).result()
