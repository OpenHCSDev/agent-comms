"""Threads: declaration and persistence owners."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Self

from .channel_targets import Tag
from .errors import RelationViolationError, UnregisteredThreadError
from .field_codec import FieldCodec
from .goals import Goal
from .thread_identity import OwnerIdentity, ThreadIncarnation, ThreadRole, TurnIdentity
from .turn_lease import ActiveTurn, TurnLeaseFence


class _GeneratedCreationTime(float):
    """Transient marker for default timestamps; never persisted as claim authority."""


def _thread_creation_time() -> float:
    return _GeneratedCreationTime(time.time())


@dataclass(frozen=True, slots=True)
class Thread:
    """Declares one agent thread's identity and provenance."""

    name: str
    tags: frozenset[str]
    worktree: str
    parent: str | None = None
    task: str | None = None
    pid: int = 0
    session_file: str | None = None
    model: str | None = None
    thinking_level: str | None = None
    goal: Goal | None = None
    created_at: float = field(default_factory=_thread_creation_time)
    _generated_created_at: bool = field(init=False, default=False, repr=False, compare=False)
    previous_worktrees: tuple[str, ...] = ()
    auto_title_pending: bool = False
    title: str | None = None
    role: ThreadRole = ThreadRole.AGENT
    active_turn: ActiveTurn | None = None
    last_goal_report_turn: str | None = None
    channel_scope_generation: int = 0
    turn_generation: int = 0
    last_finished_turn_id: str | None = None

    def __post_init__(self) -> None:
        generated = isinstance(self.created_at, _GeneratedCreationTime)
        object.__setattr__(self, "_generated_created_at", generated)
        if generated:
            object.__setattr__(self, "created_at", float(self.created_at))
        object.__setattr__(self, "role", ThreadRole(self.role))
        if self.active_turn is not None:
            if self.active_turn.owner_pid != self.pid:
                raise RelationViolationError("A turn must belong to the registered executor.")
            if self.active_turn.turn_generation is not None and (
                type(self.active_turn.turn_generation) is not int
                or self.active_turn.turn_generation <= 0
                or self.active_turn.turn_generation != self.turn_generation
            ):
                raise RelationViolationError("Active turn generation differs from its owner.")
        if self.last_goal_report_turn is not None and not isinstance(
            self.last_goal_report_turn, str
        ):
            raise ValueError("Last goal report turn must be a string or null.")
        if (
            type(self.channel_scope_generation) is not int
            or not 0 <= self.channel_scope_generation < 1 << 63
        ):
            raise ValueError("Channel scope generation must be a nonnegative 63-bit integer.")
        if type(self.turn_generation) is not int or not 0 <= self.turn_generation < 1 << 63:
            raise ValueError("Turn generation must be an exact nonnegative 63-bit integer.")
        if self.last_finished_turn_id is not None and (
            type(self.last_finished_turn_id) is not str
            or not self.last_finished_turn_id
            or self.turn_generation == 0
        ):
            raise ValueError("Finished turn requires a prior turn generation and ID.")
        for tag in self.tags:
            Tag(tag)
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not self.name or not set(self.name) <= allowed:
            raise ValueError(
                f"Thread name {self.name!r} must be alphanumeric with hyphens/underscores."
            )
        if self.parent == self.name:
            raise RelationViolationError(f"Thread {self.name!r} cannot be its own parent.")
        if not self.worktree:
            raise ValueError("Thread worktree cannot be empty.")
        if self.model is not None and not self.model.strip():
            raise ValueError("Thread model cannot be empty.")
        if self.thinking_level is not None and self.thinking_level not in {
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("Unknown thinking level.")

    @staticmethod
    @lru_cache(maxsize=512)
    def session_created_at(session_file: str) -> float:
        # Pi session headers are immutable; avoid reopening legacy transcripts
        # on every registry lookup before their creation date is persisted.
        with Path(session_file).open("rb") as stream:
            line = stream.readline(8192)
        try:
            header = json.loads(line)
            if header.get("type") == "session" and header.get("timestamp"):
                return datetime.fromisoformat(
                    header["timestamp"].replace("Z", "+00:00")
                ).timestamp()
        except (ValueError, TypeError, AttributeError):
            pass
        return 0.0

    @staticmethod
    def registry_created_at(data: Mapping) -> float:
        """Read old session headers when a registry predates creation timestamps."""
        if "created_at" in data:
            return float(data["created_at"])
        if session_file := data.get("session_file"):
            try:
                return Thread.session_created_at(session_file)
            except OSError:
                pass
        # Unknown legacy creation dates sort oldest, never by a mutable heartbeat.
        return 0.0

    @classmethod
    def from_registry(cls, name: str, data: Mapping, root: Path) -> Self:
        """Decode fields from their declaration, preserving old document defaults."""
        hints = FieldCodec._types(cls)
        special = {
            "name": name,
            "created_at": cls.registry_created_at(data),
            "tags": frozenset(data.get("tags", [])),
            "worktree": data.get("worktree", ""),
            "goal": Goal.from_registry(data["goal"], root) if data.get("goal") else None,
            "active_turn": ActiveTurn.from_wire(data["active_turn"])
            if data.get("active_turn")
            else None,
            "auto_title_pending": bool(data.get("auto_title_pending", False)),
        }
        return cls(
            **{
                declaration.name: special[declaration.name]
                if declaration.name in special
                else FieldCodec.decode(hints[declaration.name], data[declaration.name])
                for declaration in fields(cls)
                if declaration.init and (declaration.name in special or declaration.name in data)
            }
        )

    @property
    def incarnation(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.name, self.created_at)

    def owner_identity(self, generation: int) -> OwnerIdentity:
        return OwnerIdentity(self.incarnation, generation)

    @property
    def turn_identity(self) -> TurnIdentity | None:
        turn = self.active_turn
        if turn is None or turn.admission_generation is None or turn.turn_generation is None:
            return None
        return TurnIdentity(self.incarnation, turn.turn_generation)

    @property
    def turn_lease(self) -> TurnLeaseFence | None:
        """Capture this snapshot's exact turn; historical unattested turns have no lease."""
        identity = self.turn_identity
        if identity is None:
            return None
        assert self.active_turn is not None
        assert self.active_turn.admission_generation is not None
        return TurnLeaseFence(
            identity=identity,
            turn_id=self.active_turn.id,
            admission_generation=self.active_turn.admission_generation,
        )

    @property
    def is_fork(self) -> bool:
        return self.parent is not None

    @property
    def executing(self) -> bool:
        return self.active_turn is not None

    def to_wire(self) -> dict[str, object]:
        """Schema-derived projection at a JSON boundary, not a hand-maintained mirror."""
        values = asdict(self)
        values.pop("_generated_created_at")  # construction provenance is not durable authority
        return {
            **values,
            "tags": sorted(self.tags),
            "active_turn": self.active_turn.to_wire() if self.active_turn else None,
            "goal": self.goal.to_wire() if self.goal else None,
        }


def current_thread() -> Thread:
    """Declare the current thread from process environment.

    Fail-closed: raises if neither PI_AGENT_ID nor AGENT_COMMS_THREAD is set.
    """
    name = os.environ.get("PI_AGENT_ID") or os.environ.get("AGENT_COMMS_THREAD")
    if not name:
        raise UnregisteredThreadError(
            "PI_AGENT_ID is not set. This process is not an orchestrated thread."
        )
    tag_env = os.environ.get("PI_AGENT_TAGS") or os.environ.get("AGENT_COMMS_TAGS", "")
    tags = frozenset(t.strip() for t in tag_env.split(",") if t.strip())
    return Thread(
        name=name,
        tags=tags,
        worktree=os.environ.get("PI_WORKTREE", os.getcwd()),
        parent=os.environ.get("PI_PARENT_ID"),
        task=os.environ.get("PI_TASK"),
        pid=os.getpid(),
    )
