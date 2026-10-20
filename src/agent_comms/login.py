"""Launch Pi's native provider-login UI for ACP terminal authentication."""

import os
import re
import subprocess
from dataclasses import replace
from .config_options import ConfigOptions
from .launch_runtime import LaunchRecord, LoginLaunchRecord


def run_login(provider: str = "") -> int:
    """Run the login UI on the same verified native package the threads run."""
    if provider and not re.fullmatch(r"[a-zA-Z0-9_-]+", provider):
        raise ValueError("Invalid provider identifier")
    package = ConfigOptions.configured().package
    record = LoginLaunchRecord.from_record(LaunchRecord.current(), login_provider=provider)
    env = record.runtime.environment(
        package.node_environment(os.environ), configuration=record.configuration
    )
    env.update(record.configuration.encode_native())
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
    with record.arguments() as arguments:
        return subprocess.call((*command, *arguments), env=env, pass_fds=arguments.pass_fds)
