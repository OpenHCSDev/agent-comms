"""Keep an old-format practice copy alive while the staged runtime carries it.

Run with the installed interpreter. Its parent releases it through stdin once
all staged readers have finished with the copy.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from install_gate import PracticeCopy

from agent_comms.comms import Comms


def main() -> None:
    root, into = (Path(value) for value in sys.argv[1:3])
    with PracticeCopy(source=Comms(root), into=into).open() as (_, measurement):
        print(json.dumps(asdict(measurement)), flush=True)
        sys.stdin.readline()


if __name__ == "__main__":
    main()
