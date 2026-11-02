#!/usr/bin/env python3
"""Claude Code hooks for a session working on the team's repositories.

A fresh, cleared or compacted session reads both papers and every rule before any other tool
call. One script owns the whole gate so its pieces cannot drift:

  rule_gate.py deal    SessionStart: write this session's reading list and show it
  rule_gate.py gate    PreToolUse:   while reading is owed, allow only Reads of listed files
  rule_gate.py record  PostToolUse:  a successful Read marks the lines it returned
  rule_gate.py fg      PreToolUse:   refuse a foreground Bash call that asks for over 15 s

A file counts once every line of it has been read by a Read that succeeded. Subagents carry an
agent_id and are never gated. Any failure inside a hook allows the call: a gate bug must never
lock a session.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

RULE_ORDER = (
    "spine", "self-protection", "after-compaction", "unnecessary", "correct-maintenance",
    "slop-and-polymorphism", "owner-authority", "evidence-not-claims", "plain-reporting",
    "refactor-game", "no-blocking", "finish-line", "fork-orders", "tests-and-commands",
    "ship-and-install", "hotfix", "storage-and-cleanup", "send-backs-and-strikes",
    "blunt-reviews", "coordination", "self-correction", "deadlines",
)
ASKS = Path.home() / ".claude" / "tristan-asks.md"
READ_LIMIT = 2000  # the Read tool's default line count
FOREGROUND_MS = 15000


def team_directory() -> Path:
    """This checkout's team package, or the agent-comms checkout beside another repository."""
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.cwd()))
    for root in (project, project.parent / "agent-comms", Path("/home/user/agent-comms")):
        team = root / "src" / "agent_comms" / "team"
        if (team / "rules").is_dir():
            return team
    raise FileNotFoundError("no agent-comms checkout holds src/agent_comms/team/rules")


def reading_list() -> list[Path]:
    team = team_directory()
    papers = [path for paper in ("semantic-completeness", "less-is-harder")
              for path in sorted((team / "paper" / paper).glob("*.md"))]
    rules = [team / "rules" / f"{name}.md" for name in RULE_ORDER]
    listed = {*RULE_ORDER, "ally"}
    rules += sorted(path for path in (team / "rules").glob("*.md") if path.stem not in listed)
    extra = [team / "patterns.md", team / "rules" / "ally.md", ASKS]
    return [path.resolve() for path in papers + rules + extra if path.is_file()]


def marker(event: dict) -> Path:
    state = Path.home() / ".claude" / "state"
    state.mkdir(parents=True, exist_ok=True)
    return state / f"rule-gate-{event.get('session_id', 'session')}.json"


def load(event: dict) -> dict[str, list[list[int]]] | None:
    path = marker(event)
    return json.loads(path.read_text()) if path.is_file() else None


def lines_in(path: str) -> int:
    with open(path, "rb") as handle:
        return max(1, sum(1 for _ in handle))


def unread(path: str, spans: list[list[int]]) -> list[tuple[int, int]]:
    """The line ranges of a file no successful Read has returned yet."""
    gaps, cursor = [], 1
    for start, end in sorted(spans):
        if start > cursor:
            gaps.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    total = lines_in(path)
    if cursor <= total:
        gaps.append((cursor, total))
    return gaps


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))


def launched_by_core() -> bool:
    """A session Core launched (claude-comms) carries the deal in its system prompt and answers
    Core's own audit; this gate stands down for it rather than gate it twice."""
    sys.path.insert(0, str(team_directory().parent.parent))
    from agent_comms_hooks import LaunchedPrompt
    return LaunchedPrompt.variable in os.environ


def deal(event: dict) -> None:
    if event.get("agent_id") or launched_by_core():
        return
    files = reading_list()
    marker(event).write_text(json.dumps({str(path): [] for path in files}))
    print(
        f"Session {event.get('source', 'start')}: Tristan's standing order. Before ANY other tool "
        "call, read both papers, then every rule, in this order, and engage with them seriously. "
        "After a compaction, calibrate against what you did before it; the summary is not to be "
        "trusted. A gate refuses every other tool until every line of these is read (parallel "
        "Reads are fine):\n" + "\n".join(map(str, files)))


def gate(event: dict) -> None:
    if event.get("agent_id") or (owed := load(event)) is None:
        return
    if event.get("tool_name") == "Read":
        path = str(Path((event.get("tool_input") or {}).get("file_path", "")).resolve())
        if path in owed:
            return
    remaining = [path for path, spans in owed.items() if unread(path, spans)]
    deny(f"Rule reading owed: {len(remaining)} of {len(owed)} files still have unread lines. "
         "Read them before anything else, papers first, then rules. Next: "
         + ", ".join(remaining[:8]))


def record(event: dict) -> None:
    if event.get("agent_id") or (owed := load(event)) is None:
        return
    arguments = event.get("tool_input") or {}
    path = str(Path(arguments.get("file_path", "")).resolve())
    if path not in owed:
        return
    response = event.get("tool_response")
    if isinstance(response, dict) and (response.get("is_error") or response.get("error")):
        return
    start = int(arguments.get("offset") or 1)
    owed[path].append([start, start + int(arguments.get("limit") or READ_LIMIT) - 1])
    if any(unread(file, spans) for file, spans in owed.items()):
        marker(event).write_text(json.dumps(owed))
        return
    marker(event).unlink()
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": "Rule reading complete: every listed line is read. The gate is open."}}))


def foreground(event: dict) -> None:
    arguments = event.get("tool_input") or {}
    if not arguments.get("run_in_background") and float(arguments.get("timeout") or 0) > FOREGROUND_MS:
        deny("Foreground commands are capped at 15 s (Tristan, 2026-11-02). "
             "Rerun with run_in_background: true.")


def main() -> None:
    try:
        event = json.load(sys.stdin)
        {"deal": deal, "gate": gate, "record": record, "fg": foreground}[sys.argv[1]](event)
    except Exception as error:  # a gate bug must allow the call, never lock the session
        print(f"rule_gate {sys.argv[1:]}: {type(error).__name__}: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
