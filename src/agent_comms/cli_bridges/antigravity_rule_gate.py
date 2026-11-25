"""Antigravity CLI and cloud leaf of the rule gate (agent_comms.team.rule_deal).

Antigravity writes one `CHECKPOINT` line to its JSONL transcript whenever it compacts context
and invokes lifecycle hooks from `~/.gemini/config/hooks.json` or `.agents/hooks.json` with
`transcriptPath`, `cwd`, `workspacePaths` and `toolCall`. This module reuses `DealtRuleGate`,
`DeckReader`, `AuditDue` and `CompactedDeal` directly so Antigravity sessions receive the exact
same startup and post-compaction rule deal as Pi and Claude Code.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from agent_comms.cli_bridges.antigravity_transcript import AntigravityJsonLines
from agent_comms.comms import client_wire
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.projects.repo import Repo
from agent_comms.storage.store_files import AtomicFile
from agent_comms.team.rule_deal import (
    AuditDue,
    CompactedDeal,
    CompactionDeal,
    DealDelivery,
    DealTrigger,
    DealtRuleGate,
    EmptyDealDelivery,
    InjectedDelivery,
    Owed,
    ReadSpan,
    StartedDeal,
    UnstampedCompactionDeal,
)
from agent_comms_hooks.shell_commands import EmergencyHookStop
from agent_comms_hooks.skill import DeckReader

_CHECKPOINT_SUFFIX = ".agy-checkpoint.json"
_MAX_READ_LINES = 800
_MAX_READ_BYTES = 46_080
# Antigravity truncates an individual injected systemMessage around ~48.9 KB; splitting
# per dealt file at 40 KB keeps every injected step well below that ceiling with zero loss.
_MAX_INJECT_BYTES = 40_000
_WATCH_PROGRAMS = frozenset({"agent-comms-inbox-watch"})
_COMMS_HELPER_VERBS = frozenset({"run-watch", "wake-on", "call", "status", "list-deal", "use-thread"})


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _nonnegative_int(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def _str_value(mapping: Mapping[str, object], key: str) -> str:
    raw = mapping.get(key)
    return raw if isinstance(raw, str) else ""


def _mapping_value(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    raw = mapping.get(key)
    return raw if isinstance(raw, Mapping) else {}


def _str_tuple(mapping: Mapping[str, object], key: str) -> tuple[str, ...]:
    raw = mapping.get(key)
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(item for item in raw if isinstance(item, str) and item)


def _command_watches_bus(command_line: str) -> bool:
    """Allow persistent inbox watch, wake-on, and comms helper commands through a read deal."""
    try:
        tokens = shlex.split(command_line)
    except ValueError:
        return False
    if not tokens:
        return False
    # Strip a leading `: > log && exec` wrapper used to launch the persistent watch daemon.
    if "&&" in tokens:
        tail = tokens[tokens.index("&&") + 1 :]
        if tail and tail[0] == "exec":
            tail = tail[1:]
        tokens = tail
    if not tokens:
        return False
    head = Path(tokens[0]).name
    if head in _WATCH_PROGRAMS:
        return True
    for idx, token in enumerate(tokens):
        if Path(token).name == "comms_mcp.py":
            rest = [part for part in tokens[idx + 1 :] if not part.startswith("-")]
            return any(part in _COMMS_HELPER_VERBS for part in rest)
    return False


@dataclass(frozen=True)
class AntigravityCheckpointCursor:
    """Fast-path and compaction-boundary state stored beside one Antigravity transcript."""

    checkpoint_step: int = -1
    checkpoint_at: str = ""
    transcript_size: int = 0
    gate_open: bool = False
    deal_mtime_ns: int = 0
    bypass: bool = False
    workspace_key: tuple[str, ...] = ()

    @classmethod
    def read(cls, path: Path) -> AntigravityCheckpointCursor:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, Mapping):
            return cls()
        raw_ws = raw.get("workspace_key")
        workspace_key = (
            tuple(item for item in raw_ws if isinstance(item, str))
            if isinstance(raw_ws, list | tuple)
            else ()
        )
        return cls(
            checkpoint_step=int(raw.get("checkpoint_step", -1)),
            checkpoint_at=str(raw.get("checkpoint_at", "")),
            transcript_size=int(raw.get("transcript_size", 0)),
            gate_open=bool(raw.get("gate_open", False)),
            deal_mtime_ns=int(raw.get("deal_mtime_ns", 0)),
            bypass=bool(raw.get("bypass", False)),
            workspace_key=workspace_key,
        )

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        AtomicFile(path).write_bytes(
            json.dumps(
                {
                    "checkpoint_step": self.checkpoint_step,
                    "checkpoint_at": self.checkpoint_at,
                    "transcript_size": self.transcript_size,
                    "gate_open": self.gate_open,
                    "deal_mtime_ns": self.deal_mtime_ns,
                    "bypass": self.bypass,
                    "workspace_key": list(self.workspace_key),
                }
            ).encode("utf-8")
        )


class AntigravityToolMeaning(DeclaredFamily):
    """What one Antigravity tool means to the rule gate; each member names its tool."""

    @classmethod
    def of(cls, tool: str) -> AntigravityToolMeaning:
        return cls.member_or_default(tool, AntigravityOtherTool)()

    def judge(self, gate: AntigravityRuleGate, tool: str, args: Mapping[str, object]) -> None:
        del args
        gate.judge_tool(tool)

    def completed(self, gate: AntigravityRuleGate, args: Mapping[str, object]) -> None:
        del gate, args


class AntigravityOtherTool(AntigravityToolMeaning):
    """Every Antigravity tool not granted special handling during a rule deal."""


class AntigravityReadFileTool(AntigravityToolMeaning, declared_name="read_file"):
    """`read_file` returns at most 800 lines or 46,080 bytes per call."""

    max_lines: ClassVar[int] = _MAX_READ_LINES
    max_bytes: ClassVar[int] = _MAX_READ_BYTES

    @classmethod
    def default_span(cls) -> ReadSpan:
        return ReadSpan(1, cls.max_lines, cls.max_bytes)

    @classmethod
    def span(cls, args: Mapping[str, object]) -> ReadSpan:
        byte_offset = _nonnegative_int(args.get("ByteOffset"), 0)
        if byte_offset > 0:
            return ReadSpan(2, 0, 0)
        start = _positive_int(args.get("StartLine"), 1)
        raw_end = args.get("EndLine")
        if raw_end is None:
            limit = cls.max_lines
        else:
            end = _positive_int(raw_end, start - 1)
            limit = min(cls.max_lines, max(0, end - start + 1))
        return ReadSpan(start, limit, cls.max_bytes)

    def judge(self, gate: AntigravityRuleGate, tool: str, args: Mapping[str, object]) -> None:
        target = _str_value(args, "Path")
        if not target:
            gate.judge_tool(tool)
            return
        gate.judge_read(tool, gate.resolve_path(target))

    def completed(self, gate: AntigravityRuleGate, args: Mapping[str, object]) -> None:
        target = _str_value(args, "Path")
        if not target:
            return
        gate.record_read(gate.resolve_path(target), self.span(args))


class AntigravityWriteFileTool(AntigravityToolMeaning, declared_name="write_file"):
    """Writing the session audit itself is how a deal finishes."""

    def judge(self, gate: AntigravityRuleGate, tool: str, args: Mapping[str, object]) -> None:
        target = _str_value(args, "Path")
        if not target:
            gate.judge_tool(tool)
            return
        gate.judge_write(tool, gate.resolve_path(target))

    def completed(self, gate: AntigravityRuleGate, args: Mapping[str, object]) -> None:
        target = _str_value(args, "Path")
        if target and gate.resolve_path(target) == gate.audit.resolve():
            gate.audit_written()


class AntigravityEditFileTool(AntigravityWriteFileTool, declared_name="edit_file"):
    """Editing the audit in place is judged and completed like writing it."""


class AntigravityRunCommandTool(AntigravityToolMeaning, declared_name="run_command"):
    """Emergency hook stop and bus watch/wake commands stay open during a read deal."""

    def judge(self, gate: AntigravityRuleGate, tool: str, args: Mapping[str, object]) -> None:
        command = _str_value(args, "CommandLine")
        if EmergencyHookStop.is_whole_of(command) or _command_watches_bus(command):
            return
        gate.judge_tool(tool)


class AntigravityCallMcpTool(AntigravityToolMeaning, declared_name="call_mcp_tool"):
    """Ordinary comms MCP tools (including emergency release) stay open during a read deal."""

    def judge(self, gate: AntigravityRuleGate, tool: str, args: Mapping[str, object]) -> None:
        if _str_value(args, "ServerName") == "comms":
            return
        gate.judge_tool(tool)


@dataclass
class AntigravityRuleGate(DealtRuleGate):
    """One Antigravity conversation's rule deal, stored beside its `transcript.jsonl`."""

    transcript_path: Path
    artifact_dir: Path
    cwd: Path
    workspace_paths: tuple[Path, ...]
    denied_reason: str = field(default="", init=False)
    active_delivery: DealDelivery = field(default_factory=EmptyDealDelivery, init=False)

    def delivery(self) -> DealDelivery:
        return self.active_delivery

    @property
    def stem(self) -> Path:
        if self.transcript_path != Path():
            return self.transcript_path.with_suffix("")
        return self.artifact_dir / "rule-deal"

    @property
    def checkpoint_cursor_path(self) -> Path:
        return self.stem.with_suffix(_CHECKPOINT_SUFFIX)

    def resolve_path(self, raw: str) -> Path:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.cwd / candidate
        return candidate.resolve()

    def refuse(self, reason: str) -> None:
        self.denied_reason = f"{reason}\n\n{EmergencyHookStop.way_out}"

    def blocked(self, cause: Owed, tool: str) -> None:
        template = self.template_once(cause.record.due)
        self.refuse(cause.explain_refusal(tool, self.count_refusal(), template))

    def owner_words(self) -> tuple[str, ...]:
        if self.transcript_path == Path() or not self.transcript_path.is_file():
            return ()
        size = self.transcript_path.stat().st_size
        with self.transcript_path.open("rb") as stream:
            return AntigravityJsonLines().owner_words(stream, size, self.owner_prompts)

    def latest_checkpoint(self) -> tuple[int, str, int]:
        """Return ``(step_index, created_at, transcript_size)`` for the newest ``CHECKPOINT``."""
        if self.transcript_path == Path() or not self.transcript_path.is_file():
            return (-1, "", 0)
        size = self.transcript_path.stat().st_size
        with self.transcript_path.open("rb") as stream:
            found = AntigravityJsonLines().latest_checkpoint(stream, size)
        if found is None:
            return (-1, "", size)
        return (found[0], found[1], size)

    def team_repositories(self) -> tuple[Repo, ...]:
        try:
            with client_wire() as comms:
                return comms.registry.snapshot().team_project.unique_repositories()
        except Exception:
            return ()

    def in_team_workspace(self, repos: tuple[Repo, ...]) -> bool:
        """Gate conversations belonging to team repositories, worktrees, or active deals."""
        if self.record.exists():
            return True
        roots = (self.cwd, *self.workspace_paths)
        repo_paths = tuple(repo.checkout.resolve() for repo in repos)
        worktree_root = (Path.home() / "wt").resolve()
        for raw_root in roots:
            resolved = raw_root.resolve()
            if (resolved / "src/agent_comms/team/rules").is_dir():
                return True
            if (resolved / ".agents/hooks.json").is_file():
                return True
            if (resolved / ".agents/skills/comms-cloud-connect").is_dir():
                return True
            if resolved == worktree_root or worktree_root in resolved.parents:
                return True
            if any(resolved == repo or repo in resolved.parents for repo in repo_paths):
                return True
        return False

    def _deal_mtime_ns(self) -> int:
        try:
            return self.record.stat().st_mtime_ns
        except FileNotFoundError:
            return 0

    def _workspace_key(self) -> tuple[str, ...]:
        return (str(self.cwd), *(str(path) for path in self.workspace_paths))

    def _save_bypass_cursor(self) -> None:
        AntigravityCheckpointCursor(
            bypass=True,
            workspace_key=self._workspace_key(),
        ).write(self.checkpoint_cursor_path)

    def _save_cursor(self, step: int, at: str, size: int) -> None:
        AntigravityCheckpointCursor(
            checkpoint_step=step,
            checkpoint_at=at,
            transcript_size=size,
            gate_open=self.state().is_open(),
            deal_mtime_ns=self._deal_mtime_ns(),
        ).write(self.checkpoint_cursor_path)

    def _order_text(self, trigger: DealTrigger, prompts: tuple[str, ...]) -> str:
        parts = [
            f"{trigger.reason()} {self.delivery().told(self.current(), self.audit)}",
            self.template_once(self.current().due),
        ]
        if prompts:
            parts.append(
                "\n\n".join(
                    (
                        "WHAT TRISTAN ACTUALLY SAID LAST, verbatim, oldest first. Not your summary"
                        " of it. Your audit answers each one, and your next action serves them,"
                        " not the summary's next step:",
                        *(f"> {words}" for words in prompts),
                    )
                )
            )
        return "\n\n".join(part for part in parts if part)

    def deal_fresh(self, trigger: DealTrigger, repos: tuple[Repo, ...]) -> str:
        self.active_delivery = EmptyDealDelivery()
        prompts = self.owner_words()
        self.deal(DeckReader, AuditDue(prompts, repos), trigger)
        return self._order_text(trigger, prompts)

    def _deal_injected(self, trigger: DealTrigger, repos: tuple[Repo, ...]) -> tuple[str, ...]:
        def deliver() -> tuple[str, ...]:
            chunks, delivery = InjectedDelivery.render(
                DeckReader.sources(), max_bytes=_MAX_INJECT_BYTES
            )
            self.active_delivery = delivery
            prompts = self.owner_words()
            self.deal(DeckReader, AuditDue(prompts, repos), trigger)
            return (*chunks, self._order_text(trigger, prompts))

        return trigger.deliver(self.current().mode, deliver, ())

    def _inject_pending_unread(self) -> tuple[str, ...]:
        pending = self.state().unread_paths()
        if not pending:
            return ()
        chunks, delivery = InjectedDelivery.render(pending, max_bytes=_MAX_INJECT_BYTES)
        self.record_reads(delivery.reads())
        prompts = self.owner_words()
        record = self.current()
        parts = [
            (
                f"{record.trigger.reason()} Injected all {len(pending)} remaining dealt files"
                " directly into your system messages above and recorded them as read. Do not"
                " re-read them with a tool; the read gate is now open for those files."
            ),
            self.template_once(record.due),
        ]
        if prompts:
            parts.append(
                "\n\n".join(
                    (
                        "WHAT TRISTAN ACTUALLY SAID LAST, verbatim, oldest first. Not your summary"
                        " of it. Your audit answers each one, and your next action serves them,"
                        " not the summary's next step:",
                        *(f"> {words}" for words in prompts),
                    )
                )
            )
        return (*chunks, "\n\n".join(part for part in parts if part))

    def inject_deal(self, *, startup: bool = False) -> tuple[str, ...]:
        """Inject dealt sources directly on `SessionStart` / `PreInvocation` so static papers
        and rules arrive in context with zero tool-call round-trips."""
        repos = self.team_repositories()
        if not self.in_team_workspace(repos):
            self._save_bypass_cursor()
            return ()
        cursor = AntigravityCheckpointCursor.read(self.checkpoint_cursor_path)
        step, created_at, size = self.latest_checkpoint()
        if not self.record.exists():
            messages = self._deal_injected(StartedDeal(), repos)
            self._save_cursor(step, created_at, size)
            return messages
        if step > cursor.checkpoint_step:
            trigger: CompactionDeal = (
                CompactedDeal(created_at) if created_at else UnstampedCompactionDeal()
            )
            messages = self._deal_injected(trigger, repos)
            self._save_cursor(step, created_at, size)
            return messages
        messages = self._inject_pending_unread()
        if startup:
            self._save_cursor(step, created_at, size)
            return messages
        self._save_cursor(cursor.checkpoint_step, cursor.checkpoint_at, size)
        return messages

    def check_deal(self, *, startup: bool = False) -> str:
        """Deal without injection (used by `PreToolUse`, which cannot return `injectSteps`)."""
        repos = self.team_repositories()
        if not self.in_team_workspace(repos):
            self._save_bypass_cursor()
            return ""
        cursor = AntigravityCheckpointCursor.read(self.checkpoint_cursor_path)
        step, created_at, size = self.latest_checkpoint()
        if not self.record.exists():
            text = self.deal_fresh(StartedDeal(), repos)
            self._save_cursor(step, created_at, size)
            return text
        if step > cursor.checkpoint_step:
            trigger: CompactionDeal = (
                CompactedDeal(created_at) if created_at else UnstampedCompactionDeal()
            )
            text = self.deal_fresh(trigger, repos)
            self._save_cursor(step, created_at, size)
            return text
        if startup:
            self._save_cursor(step, created_at, size)
            return ""
        self._save_cursor(cursor.checkpoint_step, cursor.checkpoint_at, size)
        return ""

    def refresh_cursor(self) -> None:
        if not self.record.exists():
            return
        cursor = AntigravityCheckpointCursor.read(self.checkpoint_cursor_path)
        size = self.transcript_path.stat().st_size if self.transcript_path.is_file() else 0
        self._save_cursor(cursor.checkpoint_step, cursor.checkpoint_at, size)


@contextmanager
def _in_agent_cwd(cwd: Path) -> Iterator[None]:
    """Run probe checks in the agent's workspace directory, never the hooks.json folder."""
    previous = Path.cwd()
    changed = cwd.is_dir() and cwd.resolve() != previous.resolve()
    if changed:
        os.chdir(cwd)
    try:
        yield
    finally:
        if changed:
            os.chdir(previous)


def _inject_system_messages(messages: tuple[str, ...]) -> dict[str, object]:
    steps = [{"systemMessage": {"systemMessage": text}} for text in messages if text]
    return {"injectSteps": steps} if steps else {}


class AntigravityHookEvent(DeclaredFamily):
    """Lifecycle events Antigravity dispatches through `hooks.json`."""

    @classmethod
    def of(cls, name: str) -> AntigravityHookEvent:
        return cls.member_or_default(name, AntigravityOtherEvent)()

    def run(self, gate: AntigravityRuleGate, payload: Mapping[str, object]) -> dict[str, object]:
        del gate, payload
        return {}


class AntigravityOtherEvent(AntigravityHookEvent):
    """Events this gate does not intercept."""


class AntigravitySessionStartEvent(AntigravityHookEvent, declared_name="SessionStart"):
    def run(self, gate: AntigravityRuleGate, payload: Mapping[str, object]) -> dict[str, object]:
        del payload
        return _inject_system_messages(gate.inject_deal(startup=True))


class AntigravityPreInvocationEvent(AntigravityHookEvent, declared_name="PreInvocation"):
    def run(self, gate: AntigravityRuleGate, payload: Mapping[str, object]) -> dict[str, object]:
        del payload
        return _inject_system_messages(gate.inject_deal())


class AntigravityPreToolUseEvent(AntigravityHookEvent, declared_name="PreToolUse"):
    def run(self, gate: AntigravityRuleGate, payload: Mapping[str, object]) -> dict[str, object]:
        gate.check_deal()
        if not gate.record.exists():
            return {"decision": "allow"}
        call = _mapping_value(payload, "toolCall")
        tool_name = _str_value(call, "name")
        args = _mapping_value(call, "args")
        AntigravityToolMeaning.of(tool_name).judge(gate, tool_name, args)
        if gate.denied_reason:
            return {"decision": "deny", "reason": gate.denied_reason}
        return {"decision": "allow"}


class AntigravityPostToolUseEvent(AntigravityHookEvent, declared_name="PostToolUse"):
    def run(self, gate: AntigravityRuleGate, payload: Mapping[str, object]) -> dict[str, object]:
        if _str_value(payload, "error") or not gate.record.exists():
            return {}
        call = _mapping_value(payload, "toolCall")
        tool_name = _str_value(call, "name")
        args = _mapping_value(call, "args")
        AntigravityToolMeaning.of(tool_name).completed(gate, args)
        gate.refresh_cursor()
        return {}


def handle_hook(payload: Mapping[str, object]) -> dict[str, object]:
    """Handle one decoded Antigravity hook stdin JSON payload and return its stdout JSON."""
    raw_transcript = _str_value(payload, "transcriptPath")
    raw_artifact = _str_value(payload, "artifactDirectoryPath")
    raw_cwd = _str_value(payload, "cwd")
    transcript = Path(raw_transcript).expanduser() if raw_transcript else Path()
    artifact = Path(raw_artifact).expanduser() if raw_artifact else Path.cwd()
    cwd = Path(raw_cwd).expanduser() if raw_cwd else Path.cwd()
    workspaces = tuple(Path(item).expanduser() for item in _str_tuple(payload, "workspacePaths"))
    gate = AntigravityRuleGate(
        transcript_path=transcript,
        artifact_dir=artifact,
        cwd=cwd,
        workspace_paths=workspaces,
    )
    event = AntigravityHookEvent.of(_str_value(payload, "hookEventName"))
    with _in_agent_cwd(cwd):
        return event.run(gate, payload)
