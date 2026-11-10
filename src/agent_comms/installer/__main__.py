"""Keep the launcher's build policy; isolate an old launcher's target-source entry.

Current launchers select their own source before starting this module. A deployed
older launcher instead puts the target tree on PYTHONPATH. Only that entry must
return to its installed package before loading installer dependencies.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Never

import agent_comms.installer


def main() -> Never:
    from_target = Path(__file__).resolve().is_relative_to(Path.cwd())
    package = agent_comms.installer.__name__
    module = package if from_target else f"{package}.bootstrap"
    isolated = sys.flags.isolated or from_target
    flags = ("-I",) if isolated else ()
    os.execv(sys.executable, (sys.executable, *flags, "-m", module))


if __name__ == "__main__":
    main()
