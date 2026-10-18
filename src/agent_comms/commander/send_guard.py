"""PreToolUse hook: the commander reaches units only through `order` (send = tracked deadline, say = no work).

A raw `agent-comms ... send --from <commander>` to a unit bypasses the deadline board and the
wording checks. The human's thread `user` is exempt.
"""
import json, re, sys

from .config import CommanderConfig


def main(a):
    command = json.load(sys.stdin).get('tool_input', {}).get('command', '')
    me = re.escape(CommanderConfig.installed().commander)
    if re.search(r'\bsend\b[^|;&]*--from\s+' + me + r'\b', command) and 'agent-comms-commander' not in command:
        targets = set(re.findall(r'--to\s+(\S+)', command))
        if targets - {'user'}:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                  "permissionDecisionReason": "Raw send to a unit. Use agent-comms-commander order send (orders, with deadline) or order say (no work)."}}))
