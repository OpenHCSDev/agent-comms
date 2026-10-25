"""Keep an old-runtime parent alive while its child observes the runtime tree."""

import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    subprocess.run(
        [sys.executable, str(Path(__file__).with_name("runtime_process_child.py")), sys.argv[1]],
        check=True,
    )
