"""Change report evidence: file:line references, touched facts and edit counts.

The change-report message kind requires these records; the kind owns requiring
them, each record owns its own field validity, and FileLine owns the one
file:line text shape, its parsing and its existence check at a branch head.
"""

from __future__ import annotations

import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from agent_comms.errors import ToolRefusedError
from .field_codec import MalformedRecord, WireValue, boundary_decoder


class MissingSourceLine(ToolRefusedError):
    """A cited file:line is not in the checkout it must exist in; the caller cites another."""


class SourceReference(ABC):
    """A source reference either supplies its location or explains why it is required."""

    @classmethod
    @boundary_decoder
    def from_optional(cls, location: FileLine | None) -> SourceReference:
        return NoSourceReference() if location is None else location

    @abstractmethod
    def require(self, reason: str) -> FileLine: ...


class NoSourceReference(SourceReference):
    def require(self, reason: str) -> FileLine:
        raise ValueError(reason)


@dataclass(frozen=True, slots=True)
class FileLine(WireValue, SourceReference):
    """One source reference spelled ``file.py:line`` on the wire."""

    file: str
    line: int

    _line_token: ClassVar[re.Pattern[str]] = re.compile(r"(?<![\w./-])[\w./-]*\.[\w-]+:\d+\b")

    def __post_init__(self) -> None:
        if not self.file or not self.file.strip() or "\n" in self.file:
            raise ValueError(f"FileLine file must be a single nonempty path: {self.file!r}")
        if type(self.line) is not int or self.line < 1:
            raise ValueError(f"FileLine line must be a positive line number: {self.line!r}")

    def require(self, reason: str) -> FileLine:
        return self

    def same_file(self, other: FileLine, root: Path) -> bool:
        return (root / self.file).resolve() == (root / other.file).resolve()

    def require_exists(self, root: Path) -> None:
        path = root / self.file
        if path.is_file():
            with path.open() as source:
                for line, _ in enumerate(source, 1):
                    if line == self.line:
                        return
        raise MissingSourceLine(f"Source owner {path}:{self.line} does not exist.")

    def to_wire(self) -> str:
        return f"{self.file}:{self.line}"

    @classmethod
    def from_wire(cls, data: Any) -> FileLine:
        if not isinstance(data, str):
            raise MalformedRecord("Expected a 'file.py:line' string.")
        file, separator, line = data.rpartition(":")
        if not separator or not file or not line.isdigit():
            raise ValueError(f"Expected a 'file.py:line' reference, received {data!r}")
        return cls(file=file, line=int(line))

    @classmethod
    def schema(cls) -> dict[str, Any]:
        return {"type": "string", "description": "Source reference spelled file.py:line"}

    @classmethod
    def scan(cls, text: str) -> tuple[FileLine, ...]:
        """Every file:line reference a message body already declares."""
        return tuple(dict.fromkeys(
            cls.from_wire(token) for token in cls._line_token.findall(text)
        ))


@dataclass(frozen=True, slots=True)
class ChangedFact:
    """One fact a change touches, with its owner, consumers and edit count."""

    fact: str
    owner: FileLine
    consumers: tuple[FileLine, ...]
    search_command: str
    edits_before: int
    edits_after: int

    def __post_init__(self) -> None:
        if not self.fact or not self.fact.strip():
            raise ValueError("A changed fact requires its fact statement.")
        if not self.consumers:
            raise ValueError(
                f"The fact {self.fact!r} requires every consumer as file:line; "
                "none were declared."
            )
        if not self.search_command or not self.search_command.strip():
            raise ValueError(
                f"The fact {self.fact!r} requires its search command showing "
                "the declaration is single."
            )
        for name in ("edits_before", "edits_after"):
            edits = getattr(self, name)
            if type(edits) is not int or edits < 0:
                raise ValueError(f"The fact {self.fact!r} requires a nonnegative {name} count.")


@dataclass(frozen=True, slots=True)
class ChangeReport:
    """The declared evidence of a change, verified at its named branch head."""

    branch: str
    facts: tuple[ChangedFact, ...]

    def __post_init__(self) -> None:
        if not self.branch or not self.branch.strip():
            raise ValueError("A change report requires its branch whose head the references name.")
        if not self.facts:
            raise ValueError("A change report requires the facts its change touches.")

    def require_references_exist(self, worktree: Path) -> None:
        """Every owner and consumer reference exists at this declared branch head."""
        revision = _git(worktree, "rev-parse", "--verify", "--quiet",
                        f"{self.branch}^{{commit}}", branch=self.branch)
        sources: dict[str, str] = {}
        for fact in self.facts:
            for reference in (fact.owner, *fact.consumers):
                content = sources.get(reference.file)
                if content is None:
                    try:
                        content = _git(worktree, "show", f"{revision}:{reference.file}",
                                       branch=self.branch)
                    except ValueError:
                        raise ValueError(
                            f"Change report references {reference.file}, absent at "
                            f"branch head {self.branch} ({revision[:12]}) in {worktree}"
                        ) from None
                    sources[reference.file] = content
                if reference.line > len(content.splitlines()):
                    raise ValueError(
                        f"Change report references {reference.file}:{reference.line}, "
                        f"past the {len(content.splitlines())} lines present at branch "
                        f"head {self.branch} ({revision[:12]}) in {worktree}"
                    )


def _git(worktree: Path, *arguments: str, branch: str) -> str:
    """One git read against the sending worktree; failures name the branch."""
    result = subprocess.run(
        ["git", "-C", str(worktree), *arguments],
        capture_output=True, timeout=20,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() or "git failed"
        raise ValueError(
            f"Change report branch {branch!r} does not resolve in {worktree}: {detail}"
        )
    return result.stdout.decode(errors="replace").strip()
