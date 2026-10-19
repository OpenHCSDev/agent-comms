"""PreToolUse hook: the commander reaches units only through `order` (send = tracked deadline, say = no work).

A raw `agent-comms ... send --from <commander>` to a unit bypasses the deadline board and the
wording checks. Core's Todo store says who the units are; anyone else is reached directly.
"""
import json, re, sys

from . import order


def main(a):
    command = json.load(sys.stdin).get('tool_input', {}).get('command', '')
    if re.search(r'\bsend\b[^|;&]*--from\s+' + re.escape(order.ME) + r'\b', command) and 'agent-comms-commander' not in command:
        if set(re.findall(r'--to\s+(\S+)', command)) & order.units():
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                  "permissionDecisionReason": "Raw send to a unit. Use agent-comms-commander order send (orders, with deadline) or order say (no work)."}}))
