"""Record an owner's typed prompt through the existing human-message and directive owners."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from agent_comms.commander.order import Commander
from agent_comms.commander.send_guard import CommanderSession
from agent_comms.connector import DirectorCaller
from agent_comms.errors import RelationViolationError
from agent_comms_hooks import ClaudeWorkspaceHook, ClaudeHookProgram


@dataclass(frozen=True)
class CommanderPrompt(ClaudeWorkspaceHook):
    prompt: str

    def record(self, commander: Commander) -> None:
        session = CommanderSession(self.session_id, commander)
        try:
            selected = session.is_commander()
        except (FileNotFoundError, PermissionError, RelationViolationError) as error:
            print(f"agent-comms: commander prompt identity unavailable: {error}", file=sys.stderr)
            return
        if selected:
            message = commander.comms.messaging.send_user_message(
                commander.commander, self.prompt, worktree=self.cwd
            )
            DirectorCaller(message.sender).sent(commander.comms, message)


class CommanderPromptProgram(ClaudeHookProgram):
    """Only the registered commander's Claude session records human directives."""

    events = ("UserPromptSubmit",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    CommanderPrompt.read().record(Commander.read(Commander.path()))
