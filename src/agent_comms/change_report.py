"""Change report evidence: file:line references, touched facts and edit counts.

The change-report message kind requires these records; the kind owns requiring
them, each record owns its own field validity, and FileLine owns the one
file:line text shape, its parsing and its existence check at a branch head.
"""

from __future__ import annotations

import ast
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

from pydantic import JsonValue

from agent_comms.errors import MissingSourceLine

from .field_codec import (
    MalformedRecord,
    NullMember,
    RecordMember,
    SubclassUnion,
    WireValue,
)
from .repo import Checkout, GitCommandError, Repo
from .repository_name import RepositoryName

if TYPE_CHECKING:
    from .registry_document import CommanderProject
    from .todos import PullRequestLink


class SourceText(ABC):
    """Where cited source lines are read: a fetched git revision or a checkout's files."""

    @abstractmethod
    def lines(self, repository: Path, file: str) -> tuple[str, ...]: ...

    def candidates(self) -> tuple[SourceText, ...]:
        """The sources a cite may stand on; one source stands alone."""
        return (self,)

    def require_any(self, check: Callable[[SourceText], None]) -> None:
        """A cite stands when one candidate source passes its check."""
        *earlier, last = self.candidates()
        for revision in earlier:
            try:
                check(revision)
                return
            except MissingSourceLine:
                pass
        check(last)


@dataclass(frozen=True)
class WorkingTree(SourceText):
    """The files on disk in a checkout, where an edit not yet committed lives."""

    def lines(self, repository: Path, file: str) -> tuple[str, ...]:
        path = repository / file
        if not path.resolve().is_relative_to(repository.resolve()) or not path.is_file():
            raise MissingSourceLine(f'Source file {file} does not exist in {repository}.')
        return tuple(path.read_text(errors='replace').splitlines())


@dataclass(frozen=True)
class SourceRevision(SourceText):
    """One validation call captures each repository's resolved commit once."""

    _commits: dict[Path, str] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    @abstractmethod
    def resolve(self, repository: Path) -> str:
        """The commit this revision names in ``repository``."""

    def lines(self, repository: Path, file: str) -> tuple[str, ...]:
        if repository not in self._commits:
            self._commits[repository] = self.resolve(repository)
        commit = self._commits[repository]
        root = repository.absolute()
        path = (root / file).relative_to(root).as_posix()
        try:
            text = Repo.git(repository, 'show', f'{commit}:{path}')
            return tuple(text.splitlines())
        except GitCommandError as error:
            if error.source_path_absent(commit, path):
                for submodule in Checkout(repository).submodules():
                    if submodule.contains(path):
                        return submodule.lines(commit, path)
                raise MissingSourceLine(
                    f'Source file {file} does not exist at {commit}.'
                ) from error
            raise


@dataclass(frozen=True)
class FetchedRevision(SourceRevision):
    """A revision fetched from origin before it is read."""

    @property
    @abstractmethod
    def reference(self) -> str: ...

    @property
    @abstractmethod
    def fetch_refspec(self) -> str: ...

    def resolve(self, repository: Path) -> str:
        Repo.git(repository, 'fetch', 'origin', self.fetch_refspec)
        return Repo.git(
            repository, 'rev-parse', '--verify', f'{self.reference}^{{commit}}'
        ).strip()


@dataclass(frozen=True)
class BranchHeadRevision(SourceRevision):
    """A branch head in the reader's own checkout, where a unit's unpushed report stands."""

    branch: str

    def resolve(self, repository: Path) -> str:
        try:
            return Repo.git(repository, 'rev-parse', '--verify', '--quiet',
                            f'{self.branch}^{{commit}}').strip()
        except GitCommandError as error:
            if error.no_such_revision():
                raise MissingSourceLine(
                    f'Branch {self.branch!r} does not resolve in {repository}.') from None
            raise


@dataclass(frozen=True)
class CitedRevisions(SourceRevision):
    """A cite stands when any of these revisions holds it: an order on a problem with an open
    pull request cites that head's lines and main's alike. The first revision reads the rest."""

    revisions: tuple[SourceRevision, ...]

    def resolve(self, repository: Path) -> str:
        return self.revisions[0].resolve(repository)

    def lines(self, repository: Path, file: str) -> tuple[str, ...]:
        return self.revisions[0].lines(repository, file)

    def candidates(self) -> tuple[SourceRevision, ...]:
        return self.revisions


class MainRevision(FetchedRevision):
    @property
    def reference(self) -> str:
        return 'origin/main'

    @property
    def fetch_refspec(self) -> str:
        return 'main'


@dataclass(frozen=True)
class CommitRevision(FetchedRevision):
    """The commit a delivery's checks ran on."""

    commit: str

    @property
    def reference(self) -> str:
        return self.commit

    @property
    def fetch_refspec(self) -> str:
        return self.commit


@dataclass(frozen=True)
class PullRequestHeadRevision(FetchedRevision):
    link: PullRequestLink

    @property
    def reference(self) -> str:
        return f'refs/agent-comms/cited/{self.link.number}'

    @property
    def fetch_refspec(self) -> str:
        return f'+pull/{self.link.number}/head:{self.reference}'


class SourceRoots(ABC):
    """Where citations are read: the checkout an unqualified citation names, and the commander
    project whose registered repositories a qualified citation names instead."""

    project: CommanderProject

    @property
    @abstractmethod
    def order(self) -> Path:
        """The checkout an unqualified ``file.py:line`` is read in."""


@dataclass(frozen=True)
class OrderRoots(SourceRoots):
    """Citations from one checkout, an order's check directory or a unit's worktree:
    unqualified ones are read there."""

    checkout: Path
    project: CommanderProject

    @property
    def order(self) -> Path:
        return self.checkout


@dataclass(frozen=True)
class RegisteredRoots(SourceRoots):
    """Citations with no order behind them: unqualified ones need exactly one registered
    repository to name."""

    project: CommanderProject

    @property
    def order(self) -> Path:
        repositories = self.project.require().repos
        if len(repositories) != 1:
            raise ValueError("An unqualified FILE:LINE names no repository when several are "
                             "registered; cite it as owner/name:FILE:LINE.")
        return repositories[0].checkout


class CitedRepository(ABC):
    """The repository a file:line belongs to, carried by the citation itself."""

    @abstractmethod
    def source(self, roots: SourceRoots, revision: SourceText) -> tuple[Path, SourceText]:
        """The checkout and revisions this citation's lines are read from."""

    @abstractmethod
    def qualified(self, location: str) -> str: ...


@dataclass(frozen=True, slots=True)
class OrderRepository(CitedRepository):
    """An unqualified ``file.py:line`` names the order's own repository at its revisions."""

    def source(self, roots: SourceRoots, revision: SourceText) -> tuple[Path, SourceText]:
        return roots.order, revision

    def qualified(self, location: str) -> str:
        return location


@dataclass(frozen=True, slots=True)
class NamedRepository(CitedRepository):
    """``owner/name:file.py:line`` names another registered repository, read at its main:
    the order's pull requests belong to the order's repository, not this one."""

    name: RepositoryName

    def source(self, roots: SourceRoots, revision: SourceText) -> tuple[Path, SourceText]:
        return (Repo.selected(roots.project.require().repos, self.name.to_wire()).checkout,
                MainRevision())

    def qualified(self, location: str) -> str:
        return f"{self.name.to_wire()}:{location}"


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
class Declaration:
    """The lines of the declaration enclosing one source line, parsed once."""

    at: FileLine
    lines: range

    def holds(self, line: FileLine, root: Path) -> bool:
        return line.same_file(self.at, root) and line.line in self.lines


@dataclass(frozen=True, slots=True)
class FileLine(WireValue, SourceReference):
    """One source reference spelled ``file.py:line`` on the wire, or
    ``owner/name:file.py:line`` when it names another registered repository."""

    file: str
    line: int
    repository: CitedRepository = OrderRepository()

    # A prose token: an optional revision or repository prefix, then file.py:line.
    _line_token: ClassVar[re.Pattern[str]] = re.compile(
        r"(?<![\w./:~^@{}-])(?:([\w./~^@{}-]+):)?([\w./-]*\.[\w-]+:\d+)\b")

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

    def declaration(self, root: Path) -> Declaration:
        """The innermost class or def holding this line; its whole file at module level."""
        text = (root / self.file).read_text()
        spans = [range(1, text.count("\n") + 2)]
        if self.file.endswith(".py"):
            spans += (range(node.lineno, end + 1)
                      for node in ast.walk(ast.parse(text, filename=self.file))
                      if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                      for end in (cast(int, node.end_lineno),)
                      if node.lineno <= self.line <= end)
        span = min(spans, key=len)
        return Declaration(replace(self, file=str((root / self.file).resolve()),
                                   line=span.start), span)

    def require_exists(self, roots: SourceRoots, revision: SourceText) -> None:
        repository, revision = self.repository.source(roots, revision)
        revision.require_any(lambda candidate: self.require_line(repository, candidate))

    def require_line(self, repository: Path, revision: SourceText) -> None:
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

    def require_class(self, roots: SourceRoots, revision: SourceText) -> None:
        repository, revision = self.repository.source(roots, revision)
        revision.require_any(lambda candidate: self.require_class_at(repository, candidate))

    def require_class_at(self, repository: Path, revision: SourceText) -> None:
        tree = ast.parse('\n'.join(revision.lines(repository, self.file)), filename=self.file)
        if not any(isinstance(node, ast.ClassDef) and node.lineno == self.line
                   for node in ast.walk(tree)):
            raise MissingSourceLine(
                f"scope shape reference {self.to_wire()} must name a class declaration"
            )

    def to_wire(self) -> str:
        return self.repository.qualified(f"{self.file}:{self.line}")

    @classmethod
    def from_wire(cls, data: JsonValue) -> FileLine:
        if not isinstance(data, str):
            raise MalformedRecord("Expected a 'file.py:line' string.")
        location, separator, line = data.rpartition(":")
        qualifier, named, file = location.rpartition(":")
        if not separator or not file or not line.isdigit():
            raise ValueError(f"Expected a 'file.py:line' reference, received {data!r}")
        repository = NamedRepository(RepositoryName.from_wire(qualifier)) if named else OrderRepository()
        return cls(file=file, line=int(line), repository=repository)

    @classmethod
    def schema(cls) -> dict[str, JsonValue]:
        return {"type": "string", "description":
                "Source reference spelled file.py:line, or owner/name:file.py:line for another "
                "registered repository"}

    @classmethod
    def cites(cls, text: str) -> bool:
        """Whether a message body names any file:line, whichever repository it is in."""
        return bool(cls._line_token.search(text))

    @classmethod
    def scan(cls, text: str, project: CommanderProject) -> tuple[FileLine, ...]:
        """Every file:line reference a message body already declares. A prefix is the
        citation's repository only when it names a registered one; any other prefix
        (``origin/main:``, ``HEAD~1:``) is a revision spelling and the line is the order's."""
        named = {name.to_wire(): NamedRepository(name) for name in project.repository_names()}
        return tuple(dict.fromkeys(
            replace(cls.from_wire(location),
                    repository=named.get(prefix.casefold(), OrderRepository()))
            for prefix, location in cls._line_token.findall(text)
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
    def require_references_exist(self, roots: SourceRoots) -> None: ...


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

    def require_references_exist(self, roots: SourceRoots) -> None:
        """Every owner and consumer reference exists at this declared branch head, or at the
        main of the registered repository a reference names."""
        revision = BranchHeadRevision(self.branch)
        for fact in self.facts:
            for reference in (fact.owner, *fact.consumers):
                reference.require_exists(roots, revision)


@dataclass(frozen=True, slots=True)
class NoChangeReport(NullMember, ReportEvidence):
    """The message carries no change report; only the change-report kind requires one."""

    def require_references_exist(self, roots: SourceRoots) -> None:
        raise ValueError(
            "A change report requires its report: the branch, each touched fact, "
            "its one owner and every consumer as file:line, the search command "
            "showing the declaration is single, and the new-case edit counts."
        )
