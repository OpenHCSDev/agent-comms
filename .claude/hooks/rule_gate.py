#!/usr/bin/env python3
"""Claude Code hooks for a session working on the team's repositories.

A fresh, cleared or compacted session reads both papers and every rule before any other tool
call. One script owns the whole gate so its pieces cannot drift:

  rule_gate.py deal    SessionStart: write this session's reading list and show it
  rule_gate.py gate    PreToolUse:   while reading is owed, allow only Reads of listed files
  rule_gate.py record  PostToolUse:  a successful Read marks the lines it returned
  rule_gate.py fg      PreToolUse:   refuse a foreground Bash call that asks for over 15 s
  rule_gate.py idle    Stop:         refuse to end a turn while this session's background job runs
  rule_gate.py install               write these hooks into ~/.claude/settings.json (cloud setup)

Claude Code's JSON is decoded once, at the top, into a HookEvent and its ToolCall; everything
after asks those types. A file counts once every line of it has been read by a Read that
succeeded. Subagents and sessions Core launched are never gated. Any failure inside a hook
allows the call: a gate bug must never lock a session.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

RULE_ORDER = (
    "spine", "self-protection", "after-compaction", "unnecessary", "correct-maintenance",
    "slop-and-polymorphism", "owner-authority", "evidence-not-claims", "plain-reporting",
    "refactor-game", "no-blocking", "finish-line", "fork-orders", "tests-and-commands",
    "ship-and-install", "hotfix", "storage-and-cleanup", "send-backs-and-strikes",
    "blunt-reviews", "coordination", "self-correction", "deadlines",
)
ASKS = Path.home() / ".claude" / "tristan-asks.md"


def team_directory() -> Path:
    """The team package of the agent-comms checkout this script lives in: whichever session
    runs the hook, the rules it gates on are the ones beside the gate."""
    return Path(__file__).resolve().parents[2] / "src" / "agent_comms" / "team"


@dataclass(frozen=True)
class LineSpan:
    first: int
    last: int


@dataclass
class OwedFile:
    """One listed file and the spans successful Reads have returned from it."""

    path: Path
    read: list[LineSpan] = field(default_factory=list)

    def unread(self) -> tuple[LineSpan, ...]:
        gaps, cursor = [], 1
        for span in sorted(self.read, key=lambda span: span.first):
            if span.first > cursor:
                gaps.append(LineSpan(cursor, span.first - 1))
            cursor = max(cursor, span.last + 1)
        with self.path.open("rb") as handle:
            total = max(1, sum(1 for _ in handle))
        if cursor <= total:
            gaps.append(LineSpan(cursor, total))
        return tuple(gaps)


@dataclass
class Reading:
    """The session's owed reading, stored as its own file per session."""

    store: Path
    files: dict[Path, OwedFile]

    @classmethod
    def dealt(cls, store: Path) -> Reading:
        team = team_directory()
        papers = [path for paper in ("semantic-completeness", "less-is-harder")
                  for path in sorted((team / "paper" / paper).glob("*.md"))]
        rules = [team / "rules" / f"{name}.md" for name in RULE_ORDER]
        listed = {*RULE_ORDER, "ally"}
        rules += sorted(path for path in (team / "rules").glob("*.md") if path.stem not in listed)
        extra = [team / "patterns.md", team / "rules" / "ally.md", ASKS]
        files = [path.resolve() for path in papers + rules + extra if path.is_file()]
        return cls(store, {path: OwedFile(path) for path in files})

    @classmethod
    def owed(cls, store: Path) -> Reading | None:
        if not store.is_file():
            return None
        decoded = json.loads(store.read_text())
        return cls(store, {Path(path): OwedFile(Path(path), [LineSpan(*span) for span in spans])
                           for path, spans in decoded.items()})

    def save(self) -> None:
        self.store.write_text(json.dumps({
            str(path): [[span.first, span.last] for span in owed.read]
            for path, owed in self.files.items()}))

    def remaining(self) -> tuple[Path, ...]:
        return tuple(path for path, owed in self.files.items() if owed.unread())


class ToolCall:
    """A tool call Claude Code is about to make or has made; each kind answers for itself and
    registers under the tool name Claude Code gives it."""

    by_tool: ClassVar[dict[str, type[ToolCall]]] = {}
    tool: ClassVar[str] = ""

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        if cls.tool:
            ToolCall.by_tool[cls.tool] = cls

    @classmethod
    def decode(cls, arguments: dict, response: object) -> ToolCall:
        return cls()

    def allowed_while_owed(self, reading: Reading) -> bool:
        return False

    def recorded(self, reading: Reading) -> bool:
        """Mark what this call read; whether it changed the reading."""
        return False

    def too_long_in_foreground(self) -> bool:
        return False


@dataclass(frozen=True)
class ReadCall(ToolCall):
    path: Path
    span: LineSpan
    failed: bool
    tool = "Read"
    default_limit: ClassVar[int] = 2000  # the Read tool's default line count

    @classmethod
    def decode(cls, arguments: dict, response: object) -> ReadCall:
        first = int(arguments.get("offset") or 1)
        limit = int(arguments.get("limit") or cls.default_limit)
        failed = isinstance(response, dict) and bool(response.get("is_error") or response.get("error"))
        return cls(Path(arguments.get("file_path", "")).resolve(),
                   LineSpan(first, first + limit - 1), failed)

    def allowed_while_owed(self, reading: Reading) -> bool:
        return self.path in reading.files

    def recorded(self, reading: Reading) -> bool:
        owed = reading.files.get(self.path) if not self.failed else None
        if owed is None:
            return False
        owed.read.append(self.span)
        return True


@dataclass(frozen=True)
class BashCall(ToolCall):
    background: bool
    timeout_ms: float
    tool = "Bash"
    foreground_limit_ms: ClassVar[float] = 15000

    @classmethod
    def decode(cls, arguments: dict, response: object) -> BashCall:
        return cls(bool(arguments.get("run_in_background")), float(arguments.get("timeout") or 0))

    def too_long_in_foreground(self) -> bool:
        return not self.background and self.timeout_ms > self.foreground_limit_ms


class OtherCall(ToolCall):
    pass


@dataclass(frozen=True)
class HookEvent:
    """Claude Code's hook record, decoded once at this boundary."""

    session: str
    subagent: bool
    source: str
    call: ToolCall

    @classmethod
    def decode(cls, record: dict) -> HookEvent:
        kind = ToolCall.by_tool.get(str(record.get("tool_name")), OtherCall)
        call = kind.decode(record.get("tool_input") or {}, record.get("tool_response"))
        return cls(str(record.get("session_id", "session")), "agent_id" in record,
                   str(record.get("source", "start")), call)

    @property
    def store(self) -> Path:
        state = Path.home() / ".claude" / "state"
        state.mkdir(parents=True, exist_ok=True)
        return state / f"rule-gate-{self.session}.json"


def launched_by_core() -> bool:
    """A session Core launched (claude-comms) carries the deal in its system prompt and answers
    Core's own audit; this gate stands down for it rather than gate it twice."""
    sys.path.insert(0, str(team_directory().parent.parent))
    from agent_comms_hooks import LaunchedPrompt
    return LaunchedPrompt.variable in os.environ


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))


def deal(event: HookEvent) -> None:
    if event.subagent or launched_by_core():
        return
    reading = Reading.dealt(event.store)
    reading.save()
    print(
        f"Session {event.source}: Tristan's standing order. Before ANY other tool call, read "
        "both papers, then every rule, in this order, and engage with them seriously. After a "
        "compaction, calibrate against what you did before it; the summary is not to be "
        "trusted. A gate refuses every other tool until every line of these is read (parallel "
        "Reads are fine):\n" + "\n".join(map(str, reading.files)))


def gate(event: HookEvent) -> None:
    if event.subagent or (reading := Reading.owed(event.store)) is None:
        return
    if event.call.allowed_while_owed(reading):
        return
    remaining = reading.remaining()
    deny(f"Rule reading owed: {len(remaining)} of {len(reading.files)} files still have unread "
         "lines. Read them before anything else, papers first, then rules. Next: "
         + ", ".join(map(str, remaining[:8])))


def record(event: HookEvent) -> None:
    if event.subagent or (reading := Reading.owed(event.store)) is None:
        return
    if not event.call.recorded(reading):
        return
    if reading.remaining():
        reading.save()
        return
    event.store.unlink()
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": "Rule reading complete: every listed line is read. The gate is open."}}))


def foreground(event: HookEvent) -> None:
    if event.call.too_long_in_foreground():
        deny("Foreground commands are capped at 15 s (Tristan, 2026-11-02). "
             "Rerun with run_in_background: true.")


def own_background_jobs() -> int:
    """Background Bash jobs of this session still running. Claude Code runs each in a shell
    sourced from a shell snapshot, a child of the session's own process: count the snapshot
    shells whose parent is one of this hook's ancestors and that are not ancestors themselves."""
    import subprocess

    rows = [line.split(None, 2) for line in
            subprocess.run(["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True).stdout.splitlines()]
    parents = {int(row[0]): int(row[1]) for row in rows if len(row) >= 2}
    ancestors, pid = set(), os.getpid()
    # init adopts orphaned shells of sessions that died; they are never this session's jobs.
    while pid in parents and pid not in ancestors and pid > 1:
        ancestors.add(pid)
        pid = parents[pid]
    return sum(1 for row in rows if len(row) == 3 and "shell-snapshots/snapshot-" in row[2]
               and int(row[1]) in ancestors and int(row[0]) not in ancestors)


def idle(event: HookEvent) -> None:
    if event.subagent:
        return
    if own_background_jobs():
        print(json.dumps({"decision": "block", "reason": (
            "A background job of this session is still running. Waiting time is tracing time "
            "(tests-and-commands.md): trace the paths it will exercise and act on what you find; "
            "its notification resumes you.")}))


VERBS = {"deal": deal, "gate": gate, "record": record, "fg": foreground, "idle": idle}
USER_FLAG = "--user"


def install() -> None:
    """Put this gate in ~/.claude/settings.json, which loads whatever the session's project
    directory is. A cloud session started above the checkouts never loads their .claude
    settings; its environment setup script runs this once. The hooks are this checkout's own
    .claude/settings.json with the script's path made absolute, so that file stays the owner."""
    script = Path(__file__).resolve()
    project = json.loads((script.parent.parent / "settings.json").read_text())["hooks"]
    relative = '"$CLAUDE_PROJECT_DIR/.claude/hooks/rule_gate.py"'
    settings = Path.home() / ".claude" / "settings.json"
    document = json.loads(settings.read_text()) if settings.is_file() else {}
    hooks = document.setdefault("hooks", {})
    for event, groups in project.items():
        kept = [group for group in hooks.get(event, [])
                if not any("rule_gate.py" in hook.get("command", "") for hook in group.get("hooks", []))]
        hooks[event] = kept + [{**group, "hooks": [
            {**hook, "command": hook["command"].replace(relative, f'"{script}"') + f" {USER_FLAG}"}
            for hook in group["hooks"]]} for group in groups]
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(document, indent=2) + "\n")
    print(f"rule gate installed in {settings}")


def project_gates() -> bool:
    """The session's project settings already run this gate; the user-level copy stands down."""
    settings = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.cwd())) / ".claude" / "settings.json"
    return settings.is_file() and "rule_gate.py" in settings.read_text()


def main() -> None:
    if sys.argv[1] == "install":
        install()
        return
    try:
        if USER_FLAG in sys.argv and project_gates():
            return
        VERBS[sys.argv[1]](HookEvent.decode(json.load(sys.stdin)))
    except Exception as error:  # a gate bug must allow the call, never lock the session
        print(f"rule_gate {sys.argv[1:]}: {type(error).__name__}: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
