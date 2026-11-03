"""One thread's configuration answer: sessions and the Toad footer read the same object."""

from __future__ import annotations

import os
from pathlib import Path

from .acp_extension import CoordinationChangedUpdate
from .child_process import ProcessIdentity
from .registration import Registration
from .thread_management import ThreadManagement
from .agent_activity import AgentActivity
from .native_settings import NativeThreadDefaults


class ThreadConfiguration:
    """Joins a declared thread, its resolved model and its observed context usage."""

    def __init__(self, root: Path, registry: Registration, threads: ThreadManagement,
                 agents: AgentActivity) -> None:
        self.root, self.registry, self.threads, self.agents = root, registry, threads, agents

    def answer(self, name: str) -> CoordinationChangedUpdate:
        snapshot = self.registry.snapshot()
        thread = snapshot.require(name)
        info = self.agents.runtime_info_for(thread, snapshot)
        return CoordinationChangedUpdate(
            thread.incarnation.require_recorded(),
            str(self.root.resolve()),
            ProcessIdentity.capture(os.getpid()),
            thread.worktree,
            thread.model.applied(info.model).otherwise(NativeThreadDefaults.model.text),
            thread.thinking_level.wire_value(),
            thread.display_title,
            info.context_reading(),
            model_attribution=thread.model.observation_attribution(info.model_attribution()),
        )
