from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from agent_comms.nominal.field_codec import FieldCodec, WireValue
from agent_comms.projects.checkouts import Checkout, RepositoryReads
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.repository_storage import RepositoryStorage



class RepositorySource(WireValue):
    """One CLI boundary: an existing local path or explicit github:OWNER/REPO."""

    @classmethod
    def from_wire(cls, data: object) -> RepositorySource:
        value = FieldCodec.decode(str, data)
        if value.startswith("github:"):
            return GitHubRepositorySource(RepositoryName.from_wire(value.removeprefix("github:")))
        path = Checkout.path_from_input(value).expanduser().resolve()
        return LocalRepositorySource(Repo.from_origin(path))

    @classmethod
    def schema(cls) -> dict[str, JsonValue]:
        return {"type": "string", "description": "Existing repository path or github:OWNER/REPO"}

    @property
    @abstractmethod
    def github(self) -> RepositoryName: ...

    def allocations[Owner](self, rule: RepositoryStorage[Owner], thread: Owner) -> tuple[Path, ...]:
        """A local donor already exists; no new source allocation is needed."""
        return ()

    @abstractmethod
    def acquire[Owner](self, rule: RepositoryStorage[Owner], thread: Owner) -> Repo: ...

    def readable[Owner](
        self, rule: RepositoryStorage[Owner], thread: Owner, reads: RepositoryReads,
    ) -> Repo:
        """An explicit local source stays on its requested checkout and revision."""
        repository = self.acquire(rule, thread)
        return reads.repository(repository.checkout)

    def qualify[Owner](self, checkout: Path, rule: RepositoryStorage[Owner], thread: Owner) -> Repo:
        if not Checkout.git_roots(checkout):
            raise ValueError(f"Checkout {checkout} is occupied or incomplete; retained unchanged")
        repository = Repo.from_origin(checkout)
        if repository.github != self.github:
            raise ValueError(f"Checkout {checkout} has a different origin from {self.github}")
        return repository


@dataclass(frozen=True)
class LocalRepositorySource(RepositorySource):
    repository: Repo

    @property
    def github(self) -> RepositoryName:
        return self.repository.github

    def to_wire(self) -> str:
        return str(self.repository.checkout)

    def acquire[Owner](self, rule: RepositoryStorage[Owner], thread: Owner) -> Repo:
        self.qualify(self.repository.checkout, rule, thread)
        return self.repository


@dataclass(frozen=True)
class GitHubRepositorySource(RepositorySource):
    repository: RepositoryName

    @property
    def github(self) -> RepositoryName:
        return self.repository

    def to_wire(self) -> str:
        return f"github:{self.repository.to_wire()}"

    def allocations[Owner](self, rule: RepositoryStorage[Owner], thread: Owner) -> tuple[Path, ...]:
        return (rule.repository_donor(thread, self.repository),)

    def acquire[Owner](self, rule: RepositoryStorage[Owner], thread: Owner) -> Repo:
        donor, = self.allocations(rule, thread)
        return Repo.acquire_github(self.repository, donor)

    def readable[Owner](
        self, rule: RepositoryStorage[Owner], thread: Owner, reads: RepositoryReads,
    ) -> Repo:
        """Materialize only this caller's read-only donor at origin's declared default."""
        repository = super().readable(rule, thread, reads)
        with repository.acquiring():
            Repo.git(repository.checkout, "fetch", repository.remote)
            Repo.git(repository.checkout, "remote", "set-head", repository.remote, "--auto")
        branch = repository.default_branch()
        Repo.git(repository.checkout, "checkout", "--detach", f"{repository.remote}/{branch}")
        return repository

    def qualify[Owner](self, checkout: Path, rule: RepositoryStorage[Owner], thread: Owner) -> Repo:
        repository = Repo.qualified_github(checkout, self.repository)
        donor, = self.allocations(rule, thread)
        rule.require_repository_donor(thread, self.repository, donor)
        if ((checkout / '.git').is_symlink() or not (checkout / '.git').is_file()
                or Checkout(checkout).common_dir != donor / '.git'):
            raise ValueError(f'Checkout {checkout} is not linked to its goal-owned donor {donor}')
        return repository
