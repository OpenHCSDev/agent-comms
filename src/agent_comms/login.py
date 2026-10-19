"""Launch Pi's native provider-login UI for ACP terminal authentication."""

import os
import re
import subprocess

from .config_options import ConfigOptions
from .launch_runtime import ThreadLaunch


def run_login(provider: str = "") -> int:
    """Run the login UI on the same verified native package the threads run."""
    if provider and not re.fullmatch(r"[a-zA-Z0-9_-]+", provider):
        raise ValueError("Invalid provider identifier")
    package = ConfigOptions.configured().package
    env = os.environ.copy()
    ThreadLaunch.strip(env)
    env = {**package.node_environment(env), "AGENT_COMMS_LOGIN_PROVIDER": provider}
    env.pop("AGENT_COMMS_PROJECT_SOCKET", None), env.pop("AGENT_COMMS_PROJECT_REQUEST", None)
    command = [*package.node(), "--no-session", "--no-tools", "--no-extensions",
               "-e", str(package.cli.with_name("pi_login_control.mjs")), "--no-skills",
               "--no-context-files", "--no-prompt-templates", "--no-themes"]
    return subprocess.call(command, env=env)
