"""One thread's configuration answer: sessions and the Toad footer read the same object."""

from __future__ import annotations

import os
from pathlib import Path

from .acp_extension import CoordinationChangedUpdate
from .child_process import ProcessIdentity
from .registration import Registration
from .thread_identity import RecordedThreadIncarnation
from .thread_management import ThreadManagement
from .agent_activity import AgentActivity


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
