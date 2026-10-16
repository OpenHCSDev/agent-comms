"""Launch Pi's native provider-login UI for ACP terminal authentication."""

import os
import re
import subprocess
from pathlib import Path

from .native_arguments import NativeArguments
from .launch_runtime import RestartEnvironment


def run_login(provider: str = "") -> int:
    if provider and not re.fullmatch(r"[a-zA-Z0-9_-]+", provider):
        raise ValueError("Invalid provider identifier")
    env = os.environ.copy()
    for key in (
        "PI_PARENT_ID",
        "PI_TASK",
        "PI_PROMPT",
        "PI_WORKTREE",
        RestartEnvironment.owner_key,
        "AGENT_COMMS_MANAGED",
        "AGENT_COMMS_PROJECT_SOCKET",
        "AGENT_COMMS_PROJECT_REQUEST",
    ):
        env.pop(key, None)
    env["AGENT_COMMS_LOGIN_PROVIDER"] = provider
    command = [
        NativeArguments.configured_binary(environment=env),
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
