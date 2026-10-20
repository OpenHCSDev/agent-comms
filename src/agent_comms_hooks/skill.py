"""Put the commander skill and packaged rules in its session context."""

from __future__ import annotations

import json

from pydantic import ValidationError

from agent_comms.behaviour_patterns import PostmortemFailure
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.rule_file import RuleFile
from agent_comms_hooks import ClaudeHook, ClaudeHookProgram


class SkillText(ClaudeHookProgram):
    events = ("SessionStart",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    hook = ClaudeHook.read()
    text = "\n\n".join(
        (
            CommanderConfig.packaged_indictment().read_text(encoding="utf-8", newline=""),
            "# Recorded failures\n\n" + PostmortemFailure.catalog(),
            RuleFile.document(CommanderConfig.packaged_skill())[1],
            *(rule.render() for rule in RuleFile.packaged()),
        )
    )
    try:
        notice = f"Commander: {CommanderConfig.read(CommanderConfig.path()).commander}."
    except (
        FileNotFoundError,
        PermissionError,
        IsADirectoryError,
        UnicodeError,
        ValidationError,
    ):
        notice = "No commander is configured (installed configuration is missing or invalid)."
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": hook.hook_event_name,
                    "additionalContext": f"{text}\n\n{notice}",
                }
            }
        )
    )


if __name__ == "__main__":
    main()
