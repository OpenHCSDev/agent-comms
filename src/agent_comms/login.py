"""Launch Pi's native provider-login UI for ACP terminal authentication."""

import os
import re
import subprocess
from pathlib import Path


def run_login(provider: str = "") -> int:
    if provider and not re.fullmatch(r"[a-zA-Z0-9_-]+", provider):
        raise ValueError("Invalid provider identifier")
    env = os.environ.copy()
    for key in (
        "PI_AGENT_ID",
        "PI_PARENT_ID",
        "PI_TASK",
        "PI_PROMPT",
        "PI_WORKTREE",
        "AGENT_COMMS_THREAD",
        "AGENT_COMMS_MANAGED",
    ):
        env.pop(key, None)
    env["AGENT_COMMS_LOGIN_PROVIDER"] = provider
    command = [
        env.get("AGENT_COMMS_AGENT_BIN", "pi"),
        "--no-session",
        "--no-tools",
        "--no-extensions",
        "-e",
        str(Path(__file__).with_name("pi_login_control.ts")),
        "--no-skills",
        "--no-context-files",
        "--no-prompt-templates",
        "--no-themes",
    ]
    return subprocess.call(command, env=env)
