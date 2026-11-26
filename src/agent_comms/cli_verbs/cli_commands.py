"""Shared CLI argument mixins, parsers, and small cross-verb result records."""

from __future__ import annotations

import argparse
import re
import shlex
from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_result import CliResult, TargetEditResult
from agent_comms.execution.child_process import ProcessState
from agent_comms.execution.pi_vocabulary import ServiceTier, ThinkingLevelField
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import (
    Argument,
    RecordMember,
    boundary_decoder,
)
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_model import ThreadModel
from agent_comms.registry.thread_settings import ContextEditPolicy


def _duration_seconds(value: str) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(s|m|h|d|w)", value.strip().lower())
    if match is None:
        raise argparse.ArgumentTypeError("duration must look like 30s, 15m, 24h, 7d, or 2w")
    amount = float(match.group(1))
    if amount <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]


def _tags(value: str) -> list[str]:
    return [tag.strip() for tag in value.split(",") if tag.strip()]


@boundary_decoder
def _tag_choice_text(value: str | None) -> str | None:
    """Command-line and editor text is the given tags' wire; no text inherits."""
    return value


def _tag_text(value: frozenset[str] | None) -> str | None:
    return None if value is None else ', '.join(sorted(value))


def _shell_words(value: str) -> list[str]:
    return shlex.split(value)


def _message_reference(value: str) -> dict[str, object]:
    """CLI spelling decodes once into a wire reference."""
    sequence, message_id = value.split(":", 1)
    if int(sequence) <= 0 or not message_id:
        raise ValueError("Source must be a committed sequence:message_id reference")
    return {"seq": int(sequence), "message_id": message_id}


@dataclass(frozen=True)
class ThreadStoppedResult(RecordMember, TargetEditResult):
    stopped: str


@dataclass(frozen=True)
class ThreadArchivedResult(RecordMember, TargetEditResult):
    archived: str


@dataclass(frozen=True)
class ThreadCreatedResult(RecordMember, TargetEditResult):
    created: str
    process_identity: ProcessState

    def open_targets(self) -> tuple[str, ...]:
        return (self.created,)


@dataclass(frozen=True)
class ThreadTagsResult(RecordMember, TargetEditResult):
    thread: str
    tags: frozenset[str]


@dataclass(frozen=True)
class TeammatesResult(CliResult):
    teammates: tuple[str, ...]


@dataclass(frozen=True)
class TagRenamedResult(RecordMember, TargetEditResult):
    renamed: str
    name: str


@dataclass(frozen=True)
class ViewDeletedResult(RecordMember, TargetEditResult):
    deleted_view: str


@dataclass(frozen=True)
class TargetReadResult(RecordMember, TargetEditResult):
    read: str


@dataclass(frozen=True)
class ThreadPinnedResult(RecordMember, TargetEditResult):
    thread: str
    pinned: bool


@dataclass(frozen=True)
class UserMessageSentResult(CliResult):
    sent: MessageReference


@dataclass(frozen=True)
class ThreadConfiguredResult(RecordMember, TargetEditResult):
    configured: str
    model: ThreadModel
    thinking_level: ThinkingLevelField
    service_tier: ServiceTier
    context_edit_policy: ContextEditPolicy


@dataclass(frozen=True)
class HeartbeatResult(CliResult):
    heartbeat: str


@dataclass(frozen=True)
class ActivitySetResult(CliResult):
    activity: str
    state: str


@dataclass(frozen=True)
class OwnerReleasedResult(CliResult):
    released: str


@dataclass(frozen=True)
class LedgerMergedResult(CliResult):
    merged: bool = True


@dataclass(frozen=True, kw_only=True)
class NameArgument:
    name: Annotated[str, Argument(flags=("--name",))]


@dataclass(frozen=True, kw_only=True)
class ThreadArgument:
    thread: Annotated[str, Argument(flags=("--thread",))]


@dataclass(frozen=True, kw_only=True)
class WorktreeArgument:
    worktree: Annotated[Checkout, Argument(flags=("--worktree",))]


@dataclass(frozen=True)
class RouteReplacedResult(CliResult):
    root: str
    route: str
