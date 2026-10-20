"""Launch Pi's native provider-login UI for ACP terminal authentication."""

import os
import re
import subprocess
from pathlib import Path

from .config_options import ConfigOptions
from .launch_runtime import LaunchRecord, LoginLaunchRecord


def run_login(provider: str = "") -> int:
    """Run the login UI on the same verified native package the threads run."""
    if provider and not re.fullmatch(r"[a-zA-Z0-9_-]+", provider):
        raise ValueError("Invalid provider identifier")
    package = ConfigOptions.configured().package
    record = LoginLaunchRecord.from_record(LaunchRecord.current(), login_provider=provider)
    from .native_child import PiChild

    env = PiChild.native_environment(record, Path.cwd(), package.node_environment(dict(os.environ)))
    command = [
        *package.node(),
        "--no-session",
        "--no-tools",
        "--no-extensions",
        "-e",
        str(package.cli.with_name("pi_login_control.mjs")),
        "--no-skills",
        "--no-context-files",
        "--no-prompt-templates",
        "--no-themes",
    ]
    return subprocess.call(command, env=env)
