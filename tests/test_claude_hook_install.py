"""Install owns every Claude Code hook entry that runs its programs, tagged or not."""

import json
from importlib.metadata import entry_points

from agent_comms.harness_install import ClaudeCodeHarness

FOREIGN = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 -I guard.py"}]}


def commands(settings) -> dict[str, list[str]]:
    hooks = json.loads(settings.read_text())["hooks"]
    return {
        event: sorted(hook["command"] for entry in entries for hook in entry["hooks"])
        for event, entries in hooks.items()
    }


def test_install_adopts_untagged_copies_of_its_hooks_and_keeps_foreign_ones(tmp_path):
    settings = tmp_path / ".claude/settings.json"
    untagged: dict[str, list[dict[str, object]]] = {"PreToolUse": [FOREIGN]}
    scripts = entry_points(group="console_scripts")
    programs = ClaudeCodeHarness.hook_programs()
    for program in programs:
        entry = {key: value for key, value in program.entry(
            ClaudeCodeHarness.command(program, scripts)).items() if key != "writer"}
        for event in program.events:
            untagged.setdefault(event, []).append(entry)
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": untagged, "theme": "dark"}))

    ClaudeCodeHarness(tmp_path).configure()
    ClaudeCodeHarness(tmp_path).configure()

    expected: dict[str, list[str]] = {"PreToolUse": ["python3 -I guard.py"]}
    for program in programs:
        for event in program.events:
            expected.setdefault(event, []).append(ClaudeCodeHarness.command(program, scripts))
    assert commands(settings) == {event: sorted(found) for event, found in expected.items()}
    assert json.loads(settings.read_text())["theme"] == "dark"
