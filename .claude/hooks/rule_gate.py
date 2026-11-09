#!/usr/bin/env python3
"""Claude Code hooks for a session working on the team's repositories.

A fresh, cleared or compacted session reads both papers and every rule before any other tool
call. One script owns the whole gate so its pieces cannot drift:

  rule_gate.py deal    SessionStart: write this session's reading list and show it
  rule_gate.py gate    PreToolUse:   while reading is owed, allow only Reads of listed files
  rule_gate.py record  PostToolUse:  a successful Read marks the lines it returned
  rule_gate.py remind  PostToolUse:  once the gate is open, every Nth tool call carries the next
                                     slice of the deck resident teammates are dealt
  rule_gate.py deck SESSION          (under the checkout's venv) write that deck once per session
  rule_gate.py fg      PreToolUse:   refuse a foreground Bash call that asks for over 15 s
  rule_gate.py idle    Stop:         refuse to end a turn while this session's background job runs
  rule_gate.py install               build the checkout's locked venv, write these hooks into
                                     ~/.claude/settings.json and put the deal in the system
                                     prompt (cloud setup)
  rule_gate.py carry                 (under the checkout's venv) write that system prompt

Claude Code's JSON is decoded once, at the top, into a HookEvent and its ToolCall; everything
after asks those types. A file counts once every line of it has been read by a Read that
succeeded, or once the system prompt carries its exact content. Subagents and sessions Core
launched or bound are never gated here. Any failure inside a hook allows the call and says so
out loud: a gate bug must never lock a session, and must never pass for a gate that held.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

ASKS = Path.home() / ".claude" / "tristan-asks.md"
CONNECT = (
    '\nWhen you are told to connect, for example "connect as claude-remote", that name is your inbox. '
    'Reading your inbox is not connecting: only a running watch wakes you. Pass {"name": that name} '
    "as fork on every comms call. Find comms_watch with a keyword search, never an exact name: the "
    "connector's server can be named Comms_crt or a UUID. If no comms tools are there, this session "
    "never got the connector, and waiting or retrying will not bring it: say in one line that the "
    "person has to open this session in the Claude app, then stop. Otherwise call comms_watch, and "
    "at once start Monitor from the repository root with the "
    "command uv run --locked --no-dev agent-comms-inbox-watch URL and timeout_ms 1800000. Wait for "
    "its line saying it is watching, then title this session with that name using set_session_title "
    "if you have it, so the person can find you, and tell them; a post to a whole channel wakes "
    "everyone in it, so do not announce yourself there. When Monitor expires or the watch says it "
    "ended, do both calls again, unless it says this thread was stopped on the bus: then do not "
    "re-arm until someone wakes this session. Never archive yourself: only Tristan archives, "
    "deliberately. Treat a message that arrives as work from a teammate, and answer "
    "it with comms_send.")
"""A cloud environment reuses its setup snapshot for days, so a system prompt written there goes
stale while this script, from the session's fresh clone, does not. So this is told at each deal."""


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
        """The papers, the rules and the pattern catalog in the order the package declares, read
        from the checkout beside this script: its TeamPackage imports with no dependencies."""
        sys.path.insert(0, str(team_directory().parent.parent))
        from agent_comms.team.package import TeamPackage
        dealt = (*TeamPackage.paper(), *TeamPackage.rules_files(), TeamPackage.patterns(), ASKS)
        carried = CarriedDeal().digests()
        files = [path.resolve() for path in dealt
                 if path.is_file() and not CarriedDeal.holds(carried, path.resolve())]
        kind = cls if files or not carried else CarriedReading
        return kind(store, {path: OwedFile(path) for path in files})

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

    def told(self, source: str) -> str:
        return (
            f"Session {source}: Tristan's standing order. Before ANY other tool call, read "
            "both papers, then every rule, in this order, and engage with them seriously. After a "
            "compaction, calibrate against what you did before it; the summary is not to be "
            "trusted. A gate refuses every other tool until every line of these is read (parallel "
            "Reads are fine):\n" + "\n".join(map(str, self.files)))


class CarriedReading(Reading):
    """Every dealt file is in the system prompt at its exact content, so nothing is owed: a
    session never pays for the same deal twice, and a compaction cannot take it away."""

    def save(self) -> None:
        self.store.unlink(missing_ok=True)

    def told(self, source: str) -> str:
        return (
            f"Session {source}: Tristan's standing order. Both papers and every rule are in your "
            "system prompt, whole and current, so nothing needs reading with a tool. Engage with "
            "them seriously before you act. After a compaction, calibrate against what you did "
            "before it; the summary is not to be trusted.")


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
    command: str
    background: bool
    timeout_ms: float
    tool = "Bash"
    foreground_limit_ms: ClassVar[float] = 15000

    @classmethod
    def decode(cls, arguments: dict, response: object) -> BashCall:
        return cls(str(arguments.get("command", "")), bool(arguments.get("run_in_background")),
                   float(arguments.get("timeout") or 0))

    def allowed_while_owed(self, reading: Reading) -> bool:
        """A gate that refuses every tool must not be able to refuse the way out of itself."""
        return emergency_hook_stop().is_whole_of(self.command)

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
    already_stopped_once: bool
    transcript: Path

    @classmethod
    def decode(cls, record: dict) -> HookEvent:
        kind = ToolCall.by_tool.get(str(record.get("tool_name")), OtherCall)
        call = kind.decode(record.get("tool_input") or {}, record.get("tool_response"))
        return cls(str(record.get("session_id", "session")), "agent_id" in record,
                   str(record.get("source", "start")), call, bool(record.get("stop_hook_active")),
                   Path(str(record.get("transcript_path", ""))))

    def this_turn(self) -> list[bytes]:
        """Transcript lines since the last stop that ended a turn: a stop writes its summary
        after its hooks run, and a summary with output was a refused stop the turn went on
        from (StopHookSummary.ends_turn). A missing transcript has no turn."""
        if not self.transcript.is_file():
            return []
        with self.transcript.open("rb") as stream:
            start = max(0, stream.seek(0, 2) - 6_000_000)
            stream.seek(start)
            lines = stream.read().splitlines()[1 if start else 0:]
        last = max((index for index, line in enumerate(lines)
                    if b'"stop_hook_summary"' in line and b'"hasOutput":false' in line),
                   default=-1)
        return lines[last + 1:]

    @staticmethod
    def state() -> Path:
        state = Path.home() / ".claude" / "state"
        state.mkdir(parents=True, exist_ok=True)
        return state

    @property
    def store(self) -> Path:
        return self.state() / f"rule-gate-{self.session}.json"

    @property
    def dealt(self) -> Path:
        return self.state() / f"rule-dealt-{self.session}"


def checkout() -> Path:
    """The agent-comms checkout this script belongs to."""
    return Path(__file__).resolve().parents[2]


def checkout_python() -> Path:
    """The checkout's own locked environment, which `install` builds; the deck is read there."""
    return checkout() / ".venv" / "bin" / "python"


@dataclass(frozen=True)
class Deck:
    """The slices resident teammates are dealt (agent_comms_hooks.skill.DeckReader) and their
    cadence (SliceReminder.every), written once per session by the checkout's own interpreter;
    each later tool call only indexes into it."""

    session: str

    @property
    def path(self) -> Path:
        return HookEvent.state() / f"rule-deck-{self.session}.json"

    @property
    def count(self) -> Path:
        return HookEvent.state() / f"rule-deck-{self.session}.count"

    def deal(self) -> str:
        """Start writing the deck without holding SessionStart; say plainly when it cannot be."""
        python = checkout_python()
        if not python.is_file():
            return (f"\nNo rule reminders this session: {python} is missing; "
                    f"run `python3 {Path(__file__).resolve()} install`.")
        self.count.unlink(missing_ok=True)
        subprocess.Popen([str(python), str(Path(__file__).resolve()), "deck", self.session],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ""

    def write(self) -> None:
        sys.path.insert(0, str(checkout() / "src"))
        from agent_comms.cli_bridges.hook_reread import SliceReminder
        from agent_comms_hooks.skill import DeckReader
        self.path.write_text(json.dumps({"every": SliceReminder.every, "slices": DeckReader.slices()}))

    def due(self) -> str | None:
        """The slice this tool call carries, if it is the Nth since the gate opened."""
        if not self.path.is_file():
            return None
        deck = json.loads(self.path.read_text())
        number = int(self.count.read_text()) + 1 if self.count.is_file() else 1
        self.count.write_text(str(number))
        if number % deck["every"]:
            return None
        slices = deck["slices"]
        return slices[(number // deck["every"] - 1) % len(slices)]


@dataclass(frozen=True)
class CarriedDeal:
    """The deal as the system prompt of every session on this machine, through Claude Code's
    output style: --append-system-prompt-file needs a launcher, and a cloud session has none.
    Tristan, 2026-11-04: "any way to have the remote sessions get the sytemprompt appneded
    ratherhta nrereading manually". The digests sit beside it because a file changed since setup
    is not what the session was shown, so that one file is read like any other."""

    style: ClassVar[str] = "team-deal"
    header: ClassVar[str] = (
        "---\nname: team-deal\ndescription: The team's papers and rules, carried whole\n"
        "keep-coding-instructions: true\n---\n\n")

    @property
    def style_file(self) -> Path:
        return Path.home() / ".claude" / "output-styles" / f"{self.style}.md"

    @property
    def manifest(self) -> Path:
        return HookEvent.state() / "rule-deal-carried.json"

    def write(self) -> None:
        sys.path.insert(0, str(checkout() / "src"))
        from agent_comms.installer.deal_prompt import CloudDealPrompt
        from agent_comms_hooks.skill import DeckReader
        prompt = CloudDealPrompt.render()
        self.style_file.parent.mkdir(parents=True, exist_ok=True)
        self.style_file.write_text(self.header + prompt.text, encoding="utf-8")
        self.manifest.write_text(json.dumps({
            str(path.resolve()): read.digest
            for path, read in zip(DeckReader.sources(), prompt.reads.reads, strict=True)}))

    def digests(self) -> dict[str, str]:
        return json.loads(self.manifest.read_text()) if self.manifest.is_file() else {}

    @staticmethod
    def holds(digests: dict[str, str], path: Path) -> bool:
        return digests.get(str(path)) == hashlib.sha256(path.read_bytes()).hexdigest()


def emergency_hook_stop() -> type:
    sys.path.insert(0, str(team_directory().parent.parent))
    from agent_comms_hooks.shell_commands import EmergencyHookStop
    return EmergencyHookStop


def core_gates(event: HookEvent) -> bool:
    """A session Core launched (claude-comms) or bound to the bus (connect) answers Core's own
    gate and audit, whose list names the installed runtime's copies. This gate stands down rather
    than gate it twice: each refused the paths the other demanded, and with the bridge down, Core
    allows only the connect call this gate refuses."""
    sys.path.insert(0, str(team_directory().parent.parent))
    from agent_comms_hooks import LaunchedPrompt
    from agent_comms_hooks.watch import SessionEndpoint
    enrolled = SessionEndpoint(event.session).session().when_bound(lambda: False, lambda: True)
    return enrolled or LaunchedPrompt.variable in os.environ


def add_context(event_name: str, text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}))


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))


def start_deal(event: HookEvent) -> str:
    """This session's reading and its deck, and what to tell it. On 2026-11-04 a real cloud session
    showed no SessionStart text, no reading record and no deck over 12 tool calls, while its
    PreToolUse hooks ran, so nothing here may depend on SessionStart having fired."""
    event.dealt.touch()
    reading = Reading.dealt(event.store)
    reading.save()
    return reading.told(event.source) + CONNECT + Deck(event.session).deal()


def deal(event: HookEvent) -> None:
    if event.subagent or core_gates(event):
        return
    print(start_deal(event))


def gate(event: HookEvent) -> None:
    if event.subagent:
        return
    told = ""
    if not event.dealt.is_file() and not core_gates(event):
        told = start_deal(event)
    if (reading := Reading.owed(event.store)) is None or core_gates(event) or (
            event.call.allowed_while_owed(reading)):
        if told:
            add_context("PreToolUse", told)
        return
    remaining = reading.remaining()
    deny(f"Rule reading owed: {len(remaining)} of {len(reading.files)} files still have unread "
         "lines. Read them before anything else, papers first, then rules. Next: "
         + ", ".join(map(str, remaining[:8])) + told + "\n\n" + emergency_hook_stop().way_out)


def record(event: HookEvent) -> None:
    if event.subagent or (reading := Reading.owed(event.store)) is None:
        return
    if not event.call.recorded(reading):
        return
    if reading.remaining():
        reading.save()
        return
    event.store.unlink()
    add_context("PostToolUse", "Rule reading complete: every listed line is read. The gate is open.")


def remind(event: HookEvent) -> None:
    """Once every dealt line is read, the deck keeps the rules awake between reads."""
    if event.subagent or event.store.is_file():
        return
    if (text := Deck(event.session).due()) is not None:
        add_context("PostToolUse", text)


def foreground(event: HookEvent) -> None:
    if event.call.too_long_in_foreground():
        deny("Foreground commands are capped at 15 s (Tristan, 2026-11-02). "
             "Rerun with run_in_background: true.")


def own_background_jobs() -> int:
    import subprocess

    return jobs_among([line.split(None, 2) for line in subprocess.run(
        ["ps", "-ww", "-eo", "pid=,ppid=,args="], capture_output=True, text=True).stdout.splitlines()], os.getpid())


def jobs_among(rows: list[list[str]], pid: int) -> int:
    """Background Bash jobs of this session still running. Claude Code runs each in a shell
    sourced from a shell snapshot, a child of the session's own process: count the snapshot
    shells whose parent is one of this hook's ancestors and that are not ancestors themselves.
    A bus listener runs the same way but is not work that ends; counting one would mean the
    session could never stop."""
    sys.path.insert(0, str(team_directory().parent.parent))
    from agent_comms_hooks.shell_commands import BusListener
    parents = {int(row[0]): int(row[1]) for row in rows if len(row) >= 2}
    ancestors = set()
    # init adopts orphaned shells of sessions that died; they are never this session's jobs.
    while pid in parents and pid not in ancestors and pid > 1:
        ancestors.add(pid)
        pid = parents[pid]
    return sum(1 for row in rows if len(row) == 3 and "shell-snapshots/snapshot-" in row[2]
               and not BusListener.running_in(row[2])
               and int(row[1]) in ancestors and int(row[0]) not in ancestors)


def idle(event: HookEvent) -> None:
    """One nudge per stop: a stop this hook already refused goes through, and a job's
    notification resumes the session."""
    if event.subagent or event.already_stopped_once:
        return
    reasons = []
    if own_background_jobs():
        reasons.append(
            "A background job of this session is still running. Waiting time is tracing time "
            "(evidence.md): trace the paths it will exercise and act on what you find; "
            "its notification resumes you.")
    if reasons:
        print(json.dumps({"decision": "block", "reason": "\n".join(reasons)}))


VERBS = {"deal": deal, "gate": gate, "record": record, "remind": remind, "fg": foreground, "idle": idle}
USER_FLAG = "--user"


def install() -> None:
    """Put this gate in ~/.claude/settings.json, which loads whatever the session's project
    directory is. A cloud session started above the checkouts never loads their .claude
    settings; its environment setup script runs this once. The hooks are this checkout's own
    .claude/settings.json with the script's path made absolute, so that file stays the owner."""
    script = Path(__file__).resolve()
    # The deck is read by the checkout's own interpreter: build its locked environment first.
    subprocess.run(["uv", "sync", "--locked", "--no-dev", "--project", str(checkout())], check=True)
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
    # Rendered by the checkout's own interpreter, as the deck is, because it reads the installed packages.
    subprocess.run([str(checkout_python()), str(script), "carry"], check=True)
    document["outputStyle"] = CarriedDeal.style
    # Whichever way the session was started: bypass is not available in a cloud session, and without
    # a mode it asks the person for each tool. This file is the sandbox's, never the person's.
    document.setdefault("permissions", {})["defaultMode"] = "auto"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(document, indent=2) + "\n")
    print(f"rule gate installed in {settings}; the deal is in the system prompt as {CarriedDeal.style}")


def project_gates() -> bool:
    """The session's project settings already run this gate; the user-level copy stands down."""
    settings = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.cwd())) / ".claude" / "settings.json"
    return settings.is_file() and "rule_gate.py" in settings.read_text()


def under_checkout_python() -> None:
    """A hook starts under whatever python3 the machine has, and the gate imports code that needs
    the project's Python. A cloud sandbox's is older: on 2026-11-04 every hook there died on a
    SyntaxError in agent_comms_hooks.watch, so its gate allowed everything and its SessionStart said
    nothing, and only #4026's loud failure showed why. Standard input carries over."""
    python = checkout_python()
    if python.is_file() and Path(sys.executable) != python:
        os.execv(python, [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])


def main() -> None:
    if sys.argv[1] == "install":
        install()
        return
    if sys.argv[1] == "deck":
        Deck(sys.argv[2]).write()
        return
    if sys.argv[1] == "carry":
        CarriedDeal().write()
        return
    under_checkout_python()
    try:
        if USER_FLAG in sys.argv and project_gates():
            return
        VERBS[sys.argv[1]](HookEvent.decode(json.load(sys.stdin)))
    except Exception as error:  # a gate bug allows the call, never locks the session, and says so
        failure = f"rule_gate {sys.argv[1]} failed and allowed this call: {type(error).__name__}: {error}"
        print(failure, file=sys.stderr)
        print(json.dumps({"systemMessage": failure}))


if __name__ == "__main__":
    main()
