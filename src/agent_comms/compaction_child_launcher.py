"""Internal exec gate: native cannot run before its independent watchdog is armed.

Standalone script intentionally imports no agent runtime. Closing the gate
without sending G (including parent SIGKILL during setup) exits without exec.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    gate = int(sys.argv[1])
    try:
        armed = os.read(gate, 1) == b"G"
    finally:
        os.close(gate)
    if not armed:
        raise SystemExit(125)
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)


if __name__ == "__main__":
    main()
