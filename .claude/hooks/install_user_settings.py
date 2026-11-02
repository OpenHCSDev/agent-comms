#!/usr/bin/env python3
"""Install this checkout's Claude Code hooks as user settings (~/.claude/settings.json).

A cloud session with several repositories starts above the clones and loads no repository's
hooks, so the rule gate has to be user-level there. Call this from the environment's setup
script, which runs as root after the repositories are cloned:

    python3 /home/user/agent-comms/.claude/hooks/install_user_settings.py

The hook entries are read from this checkout's .claude/settings.json, the one declaration of
them; only "$CLAUDE_PROJECT_DIR" is replaced by this checkout's absolute path. Existing user
settings are kept: entries already pointing at this checkout's hooks are replaced, everything
else stays. Running it twice changes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[2]
PROJECT = CHECKOUT / ".claude" / "settings.json"
USER = Path.home() / ".claude" / "settings.json"
PLACEHOLDER = "$CLAUDE_PROJECT_DIR"


def ours(entry: dict) -> bool:
    return any(str(CHECKOUT / ".claude" / "hooks") in hook.get("command", "")
               for hook in entry.get("hooks", ()))


def main() -> None:
    project = json.loads(PROJECT.read_text().replace(PLACEHOLDER, str(CHECKOUT)))
    user = json.loads(USER.read_text()) if USER.is_file() else {}
    user.setdefault("env", {}).update(project.get("env", {}))
    hooks = user.setdefault("hooks", {})
    for event, entries in project.get("hooks", {}).items():
        hooks[event] = [entry for entry in hooks.get(event, []) if not ours(entry)] + entries
    USER.parent.mkdir(parents=True, exist_ok=True)
    USER.write_text(json.dumps(user, indent=2) + "\n")
    print(f"Installed {CHECKOUT}'s Claude Code hooks into {USER}")


if __name__ == "__main__":
    main()
