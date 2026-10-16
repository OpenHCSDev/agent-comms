"""Launch Pi's provider-login UI using the selected installation."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .comms import Comms
from .native_pi import NativePiRpcLaunch


def run_login(provider: str = "", *, comms: Comms) -> int:
    if provider and not re.fullmatch(r"[a-zA-Z0-9_-]+", provider):
        raise ValueError("Invalid provider identifier")
    environment = os.environ.copy()
    for key in ("PI_PARENT_ID", "PI_TASK", "PI_PROMPT", "PI_WORKTREE", "AGENT_COMMS_MANAGED"):
        environment.pop(key, None)
    environment["AGENT_COMMS_LOGIN_PROVIDER"] = provider
    launch = NativePiRpcLaunch.login(comms.runtime, package=comms.owners.native_package,
                                     worktree=Path.cwd(), environment=environment)
    return subprocess.call(launch.argv, cwd=launch.cwd, env=launch.env)
