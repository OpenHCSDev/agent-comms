"""Record bounded local test exit codes, including timeout; no silent partial pass."""

import os
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    log, seconds, *tests = sys.argv[1:]
    command = [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", *tests]
    with Path(log).open("w") as output:
        output.write("command: " + repr(command) + "\n")
        output.flush()
        try:
            status = subprocess.run(
                command,
                env=dict(os.environ, PYTHONPATH="src"),
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=int(seconds),
            ).returncode
        except subprocess.TimeoutExpired:
            status = 124
        output.write(f"\nexit: {status}\n")
    print(Path(log).read_text()[-4500:])
    sys.exit(status)
