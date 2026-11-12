#!/usr/bin/env python3
"""Record external installer calls without changing the host's packages or launchers."""
import json
from pathlib import Path
import sys

command = Path(sys.argv[0]).name
arguments = sys.argv[1:]
with (Path.home() / "commands.jsonl").open("a") as output:
    output.write(json.dumps([command, *arguments]) + "\n")

if command == "curl":
    if "-o" in arguments:
        Path(arguments[arguments.index("-o") + 1]).write_text("#!/bin/sh\nexit 0\n")
    else:
        print(Path(__file__).with_name("install-toad.sh").read_text())
