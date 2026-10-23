"""Print a Codex thread's recent trail: tool calls, their outputs, and what it said.

Usage: python -I trail.py THREAD_ID [COUNT]
"""
import glob
import json
import os
import sys

thread, count = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 40
rollouts = [p for p in glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl"))
            if thread in p]
if not rollouts:
    sys.exit(f"no rollout for thread {thread}")
rows = []
with open(max(rollouts, key=os.path.getmtime)) as fh:
    for line in fh:
        record = json.loads(line)
        payload, at = record.get("payload", {}), record.get("timestamp", "")[11:19]
        kind = payload.get("type")
        if kind in ("function_call", "custom_tool_call"):
            rows.append(f"{at} CALL {str(payload.get('arguments') or payload.get('input'))[:700]}")
        elif kind == "function_call_output":
            rows.append(f"{at} OUT  {str(payload.get('output'))[:500]}")
        elif kind == "message" and payload.get("role") == "assistant":
            said = " ".join(part.get("text", "") for part in payload.get("content", []))
            rows.append(f"{at} SAY  {said[:400]}")
print("\n".join(rows[-count:]))
