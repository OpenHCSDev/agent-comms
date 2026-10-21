"""Change report evidence: file:line references, touched facts and edit counts.

The change-report message kind requires these records; the kind owns requiring
them, each record owns its own field validity, and FileLine owns the one
file:line text shape, its parsing and its existence check at a branch head.
"""

from __future__ import annotations

import ast
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from agent_comms.errors import ToolRefusedError

from .field_codec import (
    MalformedRecord,
    NullMember,
    RecordMember,
    SubclassUnion,
    WireValue,
)
from .goals import CheckedCommit, GitCommandFailed

if TYPE_CHECKING:
    from .repo import Repo
    from .todos import PullRequestLink


class MissingSourceLine(ToolRefusedError):
    """A cited file:line is not in its declared source revision."""


@dataclass(frozen=True)
class SourceRevision(ABC):
    """One validation call captures each repository's successfully fetched commit once."""

    _commits: dict[Path, str] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    @property
    @abstractmethod
    def reference(self) -> str: ...

    @property
    @abstractmethod
    def fetch_refspec(self) -> str: ...

    def lines(self, repository: Path, file: str) -> tuple[str, ...]:
        if repository not in self._commits:
            CheckedCommit.git(str(repository), 'fetch', 'origin', self.fetch_refspec)
            self._commits[repository] = CheckedCommit.git(
                str(repository), 'rev-parse', '--verify', f'{self.reference}^{{commit}}'
            ).strip()
        commit = self._commits[repository]
        root = repository.absolute()
        path = (root / file).relative_to(root).as_posix()
        try:
            text = CheckedCommit.git(str(repository), 'show', f'{commit}:{path}')
            return tuple(text.splitlines())
        except GitCommandFailed as error:
            if error.source_path_absent(commit, path):
                raise MissingSourceLine(
                    f'Source file {file} does not exist at {commit}.'
                ) from error
            raise


class MainRevision(SourceRevision):
    @property
    def reference(self) -> str:
        return 'origin/main'

    @property
    def fetch_refspec(self) -> str:
        return 'main'


@dataclass(frozen=True)
class PullRequestHeadRevision(SourceRevision):
    link: PullRequestLink

    @property
    def reference(self) -> str:
        return f'refs/agent-comms/cited/{self.link.number}'

    @property
    def fetch_refspec(self) -> str:
        return f'+pull/{self.link.number}/head:{self.reference}'


class SourceReference(ABC):
    """A source reference either supplies its location or explains why it is required."""

    @abstractmethod
    def require(self, reason: str) -> FileLine: ...

    @abstractmethod
    def located(self) -> tuple[FileLine, ...]:
        """The source lines this reference names, for an existence check."""


@dataclass(frozen=True, slots=True)
class NoSourceReference(NullMember, SourceReference):
    """No file:line was supplied; whoever requires one refuses."""

    def require(self, reason: str) -> FileLine:
        raise ValueError(reason)

    def located(self) -> tuple[FileLine, ...]:
        return ()


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

    def located(self) -> tuple[FileLine, ...]:
        return (self,)

    def same_file(self, other: FileLine, root: Path) -> bool:
        return (root / self.file).resolve() == (root / other.file).resolve()

    def require_exists(self, repository: Path, revision: SourceRevision) -> None:
        if self.line > len(revision.lines(repository, self.file)):
            raise MissingSourceLine(
                f"Source owner {self.to_wire()} does not exist at the cited revision."
            )

    @classmethod
    def class_declaration(
        cls, declaration: type, repository: Repo, revision: SourceRevision
    ) -> FileLine:
        """Resolve a nominal module class in the same Git source its order will cite."""
        file = repository.module_file(declaration.__module__).as_posix()
        body = ast.parse('\n'.join(revision.lines(repository.checkout, file)), filename=file).body
        for name in declaration.__qualname__.split('.'):
            for node in body:
                if isinstance(node, ast.ClassDef) and node.name == name:
                    body = node.body
                    break
            else:
                raise MissingSourceLine(
                    f'Class {declaration.__qualname__} does not exist in {file} '
                    'at the cited revision.'
                )
        return cls(file, node.lineno)

    def require_class(self, repository: Path, revision: SourceRevision) -> None:
        tree = ast.parse('\n'.join(revision.lines(repository, self.file)), filename=self.file)
        if not any(isinstance(node, ast.ClassDef) and node.lineno == self.line
                   for node in ast.walk(tree)):
            raise MissingSourceLine(
                f"scope shape reference {self.to_wire()} must name a class declaration"
            )

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


class ReportEvidence(SubclassUnion, affix=""):
    """A message's change report, or none; only the change-report kind requires one."""

    @abstractmethod
    def require_references_exist(self, worktree: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class ChangeReport(RecordMember, ReportEvidence):
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


@dataclass(frozen=True, slots=True)
class NoChangeReport(NullMember, ReportEvidence):
    """The message carries no change report; only the change-report kind requires one."""

    def require_references_exist(self, worktree: Path) -> None:
        raise ValueError(
            "A change report requires its report: the branch, each touched fact, "
            "its one owner and every consumer as file:line, the search command "
            "showing the declaration is single, and the new-case edit counts."
        )
