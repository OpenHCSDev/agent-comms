"""Keep an old-runtime parent alive while its child observes the runtime tree."""

import subprocess
import sys


if __name__ == "__main__":
    subprocess.run(
        [sys.executable, "-m", "agent_comms.runtime_processes", sys.argv[1]], check=True,
    )
