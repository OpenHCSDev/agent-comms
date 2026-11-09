"""Claim a prefix as its builder and exit without completing it, as a killed install does."""

import sys
from pathlib import Path

from agent_comms.installer.building_runtime import BuildingRuntime

if __name__ == "__main__":
    BuildingRuntime.claim(Path(sys.argv[1]))
