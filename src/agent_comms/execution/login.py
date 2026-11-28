"""Launch Pi's native provider-login UI for ACP terminal authentication."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from agent_comms.config_options import ConfigOptions
from agent_comms.execution.thread_launch import ThreadLaunch


@dataclass(frozen=True)
class LoginInvocation:
    """Validated package, command and environment for a provider login.

    The construction validates the provider, resolves the installed package,
    strips thread launch state and removes project bindings. Both the existing
    suspended-terminal login and a future Qt PTY boundary consume this
    invocation without looking for another pi or copying its flags.
    """

    command: tuple[str, ...]
    env: dict[str, str]

    @classmethod
    def prepare(cls, provider: str = "") -> LoginInvocation:
        if provider and not re.fullmatch(r"[a-zA-Z0-9_-]+", provider):
            raise ValueError("Invalid provider identifier")
        package = ConfigOptions.configured().package
        env = os.environ.copy()
        ThreadLaunch.strip(env)
        env = {**package.node_environment(env), "AGENT_COMMS_LOGIN_PROVIDER": provider}
        env.pop("AGENT_COMMS_PROJECT_SOCKET", None)
        env.pop("AGENT_COMMS_PROJECT_REQUEST", None)
        command = (
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
        )
        return cls(command=command, env=env)


def run_login(provider: str = "") -> int:
    """Run the login UI on the same verified native package the threads run."""
    invocation = LoginInvocation.prepare(provider)
    return subprocess.call(list(invocation.command), env=invocation.env)
