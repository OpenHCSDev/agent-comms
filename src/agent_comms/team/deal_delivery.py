"""How dealt rule sources reach a session and which file span one read covered."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.team.prompt_reads import DealtRead, DealtSource, PreparedPrompt, PromptReads


@dataclass(frozen=True)
class ReadSpan:
    """The part of a file one read returned: from line `offset`, at most `limit` lines and
    `max_bytes` bytes. Each backend's read tool declares its own defaults."""

    offset: int
    limit: int
    max_bytes: int

    def covers(self, source: Path) -> bool:
        """Whether this read returned every line of the file."""
        text = source.read_bytes()
        return (
            self.offset <= 1
            and self.limit >= len(text.splitlines())
            and self.max_bytes >= len(text)
        )


class DealPromptRecord(ABC):
    """Minimum deal-record contract needed to render a delivery instruction."""

    @property
    @abstractmethod
    def paths(self) -> tuple[Path, ...]: ...

    @abstractmethod
    def audit_step(self, audit: Path) -> str: ...


class DealDelivery(DeclaredFamily, affix="Delivery"):
    """How a session receives the sources it is dealt. The launch identity and the gate both
    hold this member, so they cannot disagree about what entered the system prompt."""

    def launch_arguments(self) -> tuple[str, ...]:
        return ()

    open_meanwhile: ClassVar[str] = (
        "Until the deal is read and answered, the gate refuses every tool except reading these"
        " files and the audit, writing the audit, loading a tool's schema, and a Monitor running"
        " the bus watch."
    )

    def reads(self) -> tuple[DealtRead, ...]:
        """What the session already holds when its deal starts: nothing."""
        return ()

    def told(self, record: DealPromptRecord, audit: Path) -> str:
        """The order a hook gives with the deal: read every file, then answer it."""
        sources = record.paths
        return (
            f"Read each of these {len(sources)} files in full with the Read tool before any other"
            " action: they are every rule you follow and the sources they come from. A summary"
            " or memory of them does not count as reading them. Read all of them in dealt order"
            f" in one parallel call. {record.audit_step(audit)}"
            f" {self.open_meanwhile} This order is given once"
            " per session start or compaction; a short slice of them comes back inline every few"
            " tool calls.\n" + "\n".join(str(source) for source in sources)
        )


@dataclass(frozen=True)
class EmptyDealDelivery(DealDelivery):
    """The session starts without dealt sources in its system prompt."""


def _split_inline_section(title: str, text: str, max_bytes: int) -> tuple[str, ...]:
    """Split one dealt source on line boundaries so each injected message fits `max_bytes`."""
    header = f"## {title}\n\n"
    whole = header + text
    if len(whole.encode("utf-8")) <= max_bytes:
        return (whole,)
    lines = text.splitlines()
    raw_parts: list[str] = []
    # Reserve space for the numbered heading `## <title> (part X/Y)\n\n`.
    body_budget = max(1, max_bytes - len(f"## {title} (part 99/99)\n\n".encode("utf-8")))
    current: list[str] = []
    current_bytes = 0
    for line in lines:
        line_bytes = len(line.encode("utf-8"))
        sep = 1 if current else 0
        if current and current_bytes + sep + line_bytes > body_budget:
            raw_parts.append("\n".join(current))
            current = [line]
            current_bytes = line_bytes
        else:
            current.append(line)
            current_bytes += sep + line_bytes
    if current:
        raw_parts.append("\n".join(current))
    total = len(raw_parts)
    return tuple(
        f"## {title} (part {index}/{total})\n\n{part}"
        for index, part in enumerate(raw_parts, start=1)
    )


@dataclass(frozen=True)
class InjectedDelivery(DealDelivery):
    """The dealt files were injected directly into the session's context by a lifecycle hook
    (such as Antigravity `SessionStart` / `PreInvocation` `injectSteps`), so every delivered
    file counts as read at the exact SHA-256 of the bytes injected."""

    delivered: tuple[DealtRead, ...] = ()

    @classmethod
    def render(cls, paths: Iterable[Path], *, max_bytes: int) -> tuple[tuple[str, ...], Self]:
        """One read of each dealt file supplies both its injected chunks and its read digest."""
        chunks: list[str] = []
        reads: list[DealtRead] = []
        for path in paths:
            resolved = path.resolve()
            raw = resolved.read_bytes()
            source = DealtSource.of(resolved)
            reads.append(DealtRead(source, hashlib.sha256(raw).hexdigest()))
            chunks.extend(
                _split_inline_section(
                    f"{source.package}/{source.relative}",
                    raw.decode("utf-8").strip(),
                    max_bytes,
                )
            )
        return (tuple(chunks), cls(tuple(reads)))

    def reads(self) -> tuple[DealtRead, ...]:
        return self.delivered

    def told(self, record: DealPromptRecord, audit: Path) -> str:
        sources = record.paths
        return (
            f"These {len(sources)} dealt files were injected directly into your system messages"
            " above, in dealt order, and count as read: they are every rule you follow and the"
            " sources they come from. Do not re-read them with a tool unless the gate names a"
            f" file that changed. {record.audit_step(audit)}"
            f" {self.open_meanwhile}"
        )


@dataclass(frozen=True)
class SystemPromptDelivery(PreparedPrompt, DealDelivery):
    """The deal arrived in the session's system prompt, papers first, the way its launcher
    prepared it: the session read it all before its first token, with the system prompt's
    authority. It owes the witness, not the reads: the audit and "did you mean it?" still stand
    before the first tool call (Tristan, 2026-11-01)."""

    def launch_arguments(self) -> tuple[str, ...]:
        return ("--append-system-prompt", str(self.prompt.resolve(strict=True)))

    def reads(self) -> tuple[DealtRead, ...]:
        return FieldCodec.decode(PromptReads, FieldCodec.parse(self.manifest.read_bytes())).reads

    def told(self, record: DealPromptRecord, audit: Path) -> str:
        sources = record.paths
        return (
            f"These {len(sources)} files are in your system prompt, in this order, and count as"
            " read: they are every rule you follow and the sources they come from."
            f" {record.audit_step(audit)} {self.open_meanwhile} A"
            " file that changed after your prompt was prepared is not covered: read that one with"
            " the Read tool. A short slice comes back inline every few tool calls.\n"
            + "\n".join(str(source) for source in sources)
        )
