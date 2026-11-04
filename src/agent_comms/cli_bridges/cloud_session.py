"""Start a cloud session on this repository, told to connect to the bus under one name.

The fallback when no cloud session is on the bus: a session already on it starts a child with
create_session instead, which gets the comms connector at once. One started here gets no connector
until the person opens it in the Claude app; until then it can only say so.
The session is an ordinary one in the person's list, which a routine's run is not. Its inbox is
created when it connects, and the connect instructions it is dealt title it with the same name.
`claude --cloud` refuses a stream that is not a terminal, so it runs under a pseudo-terminal, which
also lets an agent start one. It runs in auto mode: bypass is not available in a cloud session, and a
session started from the CLI otherwise has no mode and asks the person for each tool.
"""
from __future__ import annotations

import argparse
import os
import pty
import sys


def prompt(name: str, task: str) -> str:
    connect = f"connect as {name}"
    return f"{connect}, then {task}" if task else connect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="The session's inbox on the bus, and its title")
    parser.add_argument("task", nargs="?", default="", help="What it does once it is connected")
    options = parser.parse_args()
    status = pty.spawn(("claude", "--cloud", prompt(options.name, options.task),
                        "--name", options.name, "--permission-mode", "auto"))
    sys.exit(os.waitstatus_to_exitcode(status))
