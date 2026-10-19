"""Put the commander skill and packaged rules in its session context."""

from __future__ import annotations

import json

from agent_comms_hooks import ClaudeHook, ClaudeHookProgram


class SkillText(ClaudeHookProgram):
    events = ("SessionStart",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    from agent_comms.commander.config import Commander, CommanderConfig
    from agent_comms.commander.send_guard import CommanderSession

    hook = ClaudeHook.read()
    config = CommanderConfig.installed()
    if not CommanderSession(hook.session_id, Commander.of(config)).is_commander():
        return
    sources = (config.packaged_skill(), *config.packaged_rules_files())
    text = "\n\n".join(
        f"# Commander file: {path.name}\n{path.read_text(encoding='utf-8', newline='')}"
        for path in sources
    )
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": hook.hook_event_name, "additionalContext": text,
    }}))


if __name__ == "__main__":
    main()
