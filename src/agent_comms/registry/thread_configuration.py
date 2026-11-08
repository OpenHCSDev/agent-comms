from __future__ import annotations

import os
from pathlib import Path

from .registration import Registration
from ..acp_extension import CoordinationChangedUpdate
from ..agent_activity import AgentActivity
from ..child_process import ProcessIdentity
from .thread_management import ThreadManagement


class ThreadConfiguration:
    """Joins a declared thread, its resolved model and its observed context usage."""

    def __init__(self, root: Path, registry: Registration, threads: ThreadManagement,
                 agents: AgentActivity) -> None:
        self.root, self.registry, self.threads, self.agents = root, registry, threads, agents

    def answer(self, name: str) -> CoordinationChangedUpdate:
        thread = self.registry.require(name)
        info = self.agents.agent_info_of(thread.name)
        return CoordinationChangedUpdate(
            thread.incarnation.require_recorded(),
            str(self.root.resolve()),
            ProcessIdentity.capture(os.getpid()),
            thread.worktree,
            self.threads.resolve_thread_model(thread.name),
            thread.thinking_level.wire_value(),
            thread.display_title,
            info.context_reading(),
        )
