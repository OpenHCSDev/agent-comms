#!/usr/bin/env python3
"""Record installer commands; no packages or virtual environments are created."""
import json
from pathlib import Path
import sys

with Path("installer-calls.jsonl").open("a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\n")
