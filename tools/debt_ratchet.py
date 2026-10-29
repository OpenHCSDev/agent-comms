#!/usr/bin/env python3
"""The debt ratchet's CI entry: agent-comms compared by the refactor-audit this checkout pins.

Every measure, the comparison, its JSON report and its exit status belong to the audit package;
agent-comms' own facts are its declaration there, audit/agent_comms_project.py, which importing
declares. This entry only reads the command line."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from agent_comms.pinned_audit import audit
from audit import agent_comms_project
from audit.ratchet import ratchet
from audit.repository import Repository


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Repository-relative production source directory")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--workers", type=int, default=os.process_cpu_count() or 1,
                        help="Pool width; a caller running ratchets in parallel narrows it")
    args = parser.parse_args()
    return ratchet(Repository(Path.cwd()), args.base, args.head, args.root, args.workers)


if __name__ == "__main__":
    raise SystemExit(main())
