"""Print a Codex thread's recent trail: tool calls, their outputs, and what it said.

Usage: python -I trail.py THREAD_ID [COUNT]

Core's rollout decoder owns the record format; this only picks the newest rollout and prints
the transcript events it yields.
"""

import sys
from pathlib import Path

from agent_comms.cli_bridges.codex_rollout import CodexRollout
from agent_comms.runtime_updates import FinishedItem

thread, count = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 40
rollouts = sorted(
    Path.home().glob(f".codex/sessions/*/*/*/rollout-*{thread}.jsonl"),
    key=lambda path: path.stat().st_mtime,
)
if not rollouts:
    sys.exit(f"no rollout for thread {thread}")
events = [
    event
    for update in CodexRollout(rollouts[-1]).read()
    if isinstance(update, FinishedItem)
    for event in update.events
]
print("\n".join(repr(event)[:700] for event in events[-count:]))
