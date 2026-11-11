from __future__ import annotations

import os
import re
import subprocess
from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import (
    Annotated,
    ClassVar,
    Self,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    InstanceOf,
    PlainSerializer,
    PlainValidator,
)

from agent_comms.installer.native_build_inputs import CloneShape, GitLaunch
from agent_comms.nominal.field_codec import FieldCodec, WireValue
from agent_comms.projects.git_errors import (
    FailedGitCommand,
    GitCommandError,
    UnavailableCommitError,
)
from agent_comms.projects.repository_name import RepositoryName


class RepositorySelection(ABC):
    """A repository choice is applied by the native project it configures."""

    @abstractmethod
    def applied(self, recorded: tuple[Repo, ...]) -> tuple[Repo, ...]: ...

    @abstractmethod
    def founded[A, F](self, absent: A, found: Callable[[tuple[Repo, ...]], F]) -> A | F:
        """What a bus without a team becomes under this choice."""


class Repo(GitLaunch, BaseModel, WireValue):
    """A repository the fleet works on: its check checkout, its GitHub name and its source root."""

    model_config = ConfigDict(frozen=True)
    remote: ClassVar[str] = "origin"
    local_remote: ClassVar[str] = "."
    full_commit: ClassVar[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

    checkout: Path
    github: InstanceOf[
        Annotated[
            RepositoryName,
            PlainValidator(RepositoryName.from_wire, json_schema_input_type=str),
            PlainSerializer(lambda name: name.to_wire()),
        ]
    ]
    source: str = "src"

    @classmethod
    def command(cls, checkout: Path | str, *args: str) -> tuple[str, ...]:
        """Only explicit acquisition may contact a promisor remote for missing objects."""
        shape = CloneShape.of(Path(checkout))
        return super().command(checkout, *shape.read_options(cls.located(os.environ)), *args)

    def to_wire(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    @classmethod
    def from_wire(cls, data: object) -> Self:
        return cls.model_validate_json(FieldCodec.compact(data))

    @classmethod
    def selection_refusal(cls) -> str:
        return "Select --repo when more than one repository is registered."

    @classmethod
    def selected(cls, repositories: tuple[Repo, ...], name: str) -> Repo:
        candidates = cls.candidates(repositories, name)
        if len(candidates) != 1:
            raise ValueError(cls.selection_refusal())
        repository, = candidates
        return repository

    @classmethod
    def candidates(cls, repositories: tuple[Repo, ...], name: str) -> tuple[Repo, ...]:
        """No name leaves every registered repository a candidate; a name selects exactly one."""
        if not name:
            return repositories
        for repository in repositories:
            if repository.named(name):
                return (repository,)
        configured = ", ".join(
            f"{repository.github.to_wire()} ({repository.checkout})"
            for repository in repositories
        )
        raise ValueError(f"Unknown repository {name!r}. Registered repositories: {configured}. "
                         "Set repositories with agent-comms install --repos.")

    @classmethod
    def declaring(cls, repositories: tuple[Repo, ...], module: str) -> Repo:
        for repository in repositories:
            if (repository.checkout / repository.module_file(module)).is_file():
                return repository
        raise ValueError(f"No registered repository holds {module}")

    def module_file(self, module: str) -> Path:
        """Where the dotted ``module`` sits under this checkout's source root, relative to it."""
        return Path(self.source, *module.split(".")).with_suffix(".py")

    def named(self, name: str) -> bool:
        """A repository name or path identifies this declared checkout, not another worktree."""
        return self.github.named(name) or self.checkout == Path(name).resolve()

    @property
    def checked_out(self) -> bool:
        """The checkout is a work tree of its own; a removed submodule leaves an empty directory
        that git would resolve to the enclosing repository instead."""
        return (self.checkout / ".git").exists()


    def default_branch(self) -> str:
        """The origin's declared default, never a guessed branch name."""
        try:
            reference = self.git(
                self.checkout, "symbolic-ref", "--short", f"refs/remotes/{self.remote}/HEAD",
            ).strip()
        except GitCommandError as error:
            raise ValueError(
                f"Origin's default branch is unset in {self.checkout}; "
                "run git remote set-head origin --auto in that checkout."
            ) from error
        return reference.removeprefix(f"{self.remote}/")

    @classmethod
    def missing_commits(cls, checkout: Path | str, *objects: str) -> tuple[str, ...]:
        """Resolve Git expressions, including abbreviated SHAs, as commit objects in one batch."""
        requested = tuple(dict.fromkeys(objects))
        if not requested:
            return ()
        answers = cls.git(checkout, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                          input="".join(f"{name}^{{commit}}\n" for name in requested))
        return tuple(name for name, answer in zip(requested, answers.splitlines(), strict=True)
                     if not answer.endswith(" commit"))

    @classmethod
    def require_local_commits(cls, checkout: Path | str, *objects: str) -> None:
        """Observation never acquires objects or mistakes an unavailable one for False."""
        missing = cls.missing_commits(checkout, *objects)
        if missing:
            raise UnavailableCommitError(checkout, missing)

    @classmethod
    def require_commits(cls, checkout: Path | str, remote: str, *objects: str) -> None:
        """The explicit acquisition boundary: fetch missing objects, then prove availability."""
        missing = cls.missing_commits(checkout, *objects)
        if missing:
            try:
                cls.git(checkout, "fetch", "--quiet", "--no-tags", "--", remote, *missing)
            except GitCommandError as error:
                raise UnavailableCommitError(checkout, missing, f"remote {remote}: {error}") from error
            cls.require_local_commits(checkout, *objects)

    def acquire(self, *objects: str) -> None:
        """Acquire exact commits from this repository's own remote."""
        self.require_commits(self.checkout, self.remote, *objects)

    @classmethod
    def unservable_commits(cls, checkout: Path | str, remote: str,
                           *objects: str) -> tuple[str, ...]:
        """Fetch each missing object alone, so one the remote refuses cannot cost the others;
        answer the ones still absent."""
        for name in cls.missing_commits(checkout, *objects):
            try:
                cls.git(checkout, "fetch", "--quiet", "--no-tags", "--", remote, name)
            except GitCommandError:
                continue
        return cls.missing_commits(checkout, *objects)

    def contains(self, sha: str, revision: str = "HEAD") -> bool:
        """Strict local ancestry; only valid local non-ancestors mean False."""
        self.require_local_commits(self.checkout, sha, revision)
        return self.descends(self.checkout, sha, revision)

    def has_commit(self, sha: str) -> bool:
        return self.answers(self.checkout, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")

    @classmethod
    def tree_objects(cls, checkout: Path | str, revision: str, paths: tuple[str, ...],
                     *options: str) -> dict[str, str]:
        """Decode selected tree entries without requiring their contents to be local."""
        entries = cls.git(checkout, "ls-tree", "-z", *options, revision, "--", *paths)
        return {path: metadata.rsplit(" ", 1)[1]
                for metadata, path in (entry.split("\t", 1)
                                       for entry in entries.split("\0") if entry)}

    @classmethod
    def blobs(cls, checkout: Path | str, *objects: str, timeout: float = GitLaunch.command_timeout) -> tuple[bytes, ...]:
        """Decode required contents once; an unavailable blob is never an absent path."""
        if not objects:
            return ()
        batch = cls.git_bytes(checkout, "cat-file", "--batch", "-Z", timeout=timeout,
                              input="".join(f"{name}\0" for name in objects))
        contents: list[bytes] = []
        offset = 0
        for name in objects:
            end = batch.index(b"\0", offset)
            header = batch[offset:end]
            offset = end + 1
            if header.endswith(b" missing"):
                raise UnavailableCommitError(checkout, (name,), "required blob unavailable")
            size = int(header.rsplit(b" ", 1)[1])
            contents.append(batch[offset:offset + size])
            offset += size + 1
        return tuple(contents)

    @classmethod
    def git_bytes(cls, checkout: Path | str, *args: str, timeout: float = GitLaunch.command_timeout,
                  input: str = "") -> bytes:
        """Git's exact output, for answers sized in bytes (``cat-file --batch``); every
        failure a GitCommandError."""
        return cls._output(checkout, cls._run(checkout, *args, timeout=timeout, input=input.encode()))

    @staticmethod
    def _output(checkout: Path | str, run: subprocess.CompletedProcess[bytes]) -> bytes:
        """The shared failure boundary for captured and streamed Git answers."""
        if run.returncode:
            raise FailedGitCommand(str(checkout), run)
        return run.stdout

    @classmethod
    def git_pipeline(cls, checkout: Path | str, producer: tuple[str, ...], consumer: tuple[str, ...],
                     *, timeout: float = GitLaunch.command_timeout, input: str = '') -> str:
        return cls._output(checkout, cls._pipe(checkout, producer, consumer,
                           timeout=timeout, input=input.encode())).decode()

    @classmethod
    def acquire_pipeline(
        cls, checkout: Path | str, producer: tuple[str, ...], consumer: tuple[str, ...],
        *, timeout: float = GitLaunch.command_timeout, input: str = '',
    ) -> str:
        """Prepare exactly the requested content under the joined acquisition lifetime.

        GitLaunch may ask its promisor for missing objects here. Repo's ordinary
        commands still pass the clone's strict read options after preparation.
        """
        with cls.acquiring():
            return cls._output(checkout, GitLaunch._pipe(
                checkout, producer, consumer, timeout=timeout, input=input.encode(),
            )).decode()

    @classmethod
    def acquire_git(cls, checkout: Path | str, *args: str, timeout: float = GitLaunch.command_timeout,
                    input: str = "") -> str:
        """Acquire a requested source answer before validation, never during observation."""
        with cls.acquiring():
            return cls._output(checkout, GitLaunch._run(
                checkout, *args, timeout=timeout, input=input.encode(),
            )).decode()

    @classmethod
    def git(cls, checkout: Path | str, *args: str, timeout: float = GitLaunch.command_timeout, input: str = "") -> str:
        """Git's output as text; every failure a GitCommandError."""
        return cls.git_bytes(checkout, *args, timeout=timeout, input=input).decode()

    @classmethod
    def answers(cls, checkout: Path | str, *args: str) -> bool:
        """A yes-or-no Git question: exit 0 is yes, exit 1 is no, and any other exit (no
        repository, an unreadable object store) is a GitCommandError, never a no."""
        run = cls._run(checkout, *args)
        if run.returncode not in (0, 1):
            raise FailedGitCommand(str(checkout), run)
        return run.returncode == 0

    @classmethod
    def descends(cls, checkout: Path | str, ancestor: str, revision: str) -> bool:
        """Whether ``revision`` holds ``ancestor`` in this checkout's history. Two full commit ids
        have one answer for good; a ref or an abbreviation moves, so it is asked every time."""
        if cls.full_commit.fullmatch(ancestor) and cls.full_commit.fullmatch(revision):
            return cls.commit_descends(cls.ancestry_dir(Path(checkout)), ancestor, revision)
        return cls.answers(checkout, "merge-base", "--is-ancestor", ancestor, revision)

    @staticmethod
    @cache
    def commit_descends(common_dir: Path, ancestor: str, descendant: str) -> bool:
        return Repo.answers(common_dir, "merge-base", "--is-ancestor", ancestor, descendant)

    @staticmethod
    @cache
    def ancestry_dir(checkout: Path) -> Path:
        """Where immutable ancestry is asked. Liveness probes read Checkout.common_dir instead,
        because a removed worktree must stop answering there."""
        return Repo.common_dir_of(checkout)

    @staticmethod
    def common_dir_of(checkout: Path) -> Path:
        """Git's shared ref and object owner, independent of this worktree's HEAD."""
        return Path(Repo.git(checkout, "rev-parse", "--path-format=absolute",
                             "--git-common-dir").strip()).resolve()

    @classmethod
    def store_serves(cls, store: Path, *args: str) -> bool:
        """A question to one submodule store, pinned as the git directory so Git never
        discovers the repository around it: any failure, a store holding no repository
        included, means the store cannot serve."""
        return cls._run(store, "--git-dir", ".", *args).returncode == 0

    @classmethod
    def grep(cls, checkout: Path | str, *args: str) -> tuple[str, ...]:
        """``git grep`` rows: exit 1 means nothing matched."""
        run = cls._run(checkout, "grep", *args)
        if run.returncode == 1:
            return ()
        if run.returncode:
            raise FailedGitCommand(str(checkout), run)
        return tuple(run.stdout.decode().splitlines())

    @classmethod
    def merge_conflicts(cls, checkout: Path | str, left: str, right: str) -> tuple[str, ...]:
        """The paths ``git merge-tree`` cannot merge between two commits: exit 0 merges
        cleanly, exit 1 lists the conflicting paths after the written tree."""
        run = cls._run(checkout, "merge-tree", "--write-tree", "--name-only", "--no-messages",
                       left, right)
        if run.returncode == 1:
            return tuple(run.stdout.decode().splitlines()[1:])
        if run.returncode:
            raise FailedGitCommand(str(checkout), run)
        return ()

    @classmethod
    @cache
    def of(cls, checkout: Path) -> Repo:
        """Its GitHub name comes from the checkout's own origin remote, which does not move while
        a process runs: installs restart the connector."""
        return cls.from_origin(checkout)

    @classmethod
    def from_origin(cls, checkout: Path) -> Repo:
        """Read this path's current origin, including across acquisition and repair."""
        url = cls.git(checkout, "remote", "get-url", cls.remote).strip()
        return cls(checkout=checkout, github=cls.repository_url(url))

    @classmethod
    def qualified_github(cls, checkout: Path, expected: RepositoryName) -> Repo:
        """A complete donor or linked tree of the explicitly requested GitHub origin.

        Read the configured origin, not a cached Repo or Git's insteadOf transport
        rewrite. Authentication/transport configuration still belongs to Git.
        """
        origin = cls.git(checkout, "config", "--get", f"remote.{cls.remote}.url").strip()
        match = re.fullmatch(
            r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?",
            origin, flags=re.IGNORECASE,
        )
        if match is None or RepositoryName.from_wire(match[1]) != expected:
            raise ValueError(f"Repository {checkout} does not have the requested GitHub origin {expected}")
        top = Path(cls.git(checkout, "rev-parse", "--show-toplevel").strip()).resolve()
        if top != checkout:
            raise ValueError(f"Repository {checkout} is not its own Git working tree")
        repository = cls(checkout=checkout, github=expected)
        branch = repository.default_branch()
        cls.require_local_commits(checkout, 'HEAD', f'origin/{branch}')
        cls.git(checkout, 'rev-list', '--objects', '--missing=error', '--quiet',
                'HEAD', f'origin/{branch}', '--')
        return repository

    @staticmethod
    def github_url(name: RepositoryName) -> str:
        return f"https://github.com/{name.to_wire()}.git"

    @classmethod
    def acquire_github(cls, name: RepositoryName, checkout: Path) -> Repo:
        """Acquire once in an exclusively created donor; preserve every failed directory."""
        if checkout.is_symlink() or checkout != checkout.resolve():
            raise ValueError(f"Repository donor {checkout} must be a physical path")
        if checkout.exists():
            if not (checkout / ".git").is_dir() or (checkout / ".git").is_symlink():
                raise ValueError(f"Repository donor {checkout} is occupied or incomplete; retained unchanged")
            try:
                repository = cls.qualified_github(checkout, name)
                if cls.common_dir_of(checkout) != checkout / '.git':
                    raise ValueError('The donor does not own its Git object store')
                return repository
            except (GitCommandError, ValueError) as error:
                raise ValueError(f"Repository donor {checkout} failed qualification; retained unchanged: {error}") from error
        checkout.parent.mkdir(parents=True, exist_ok=True)
        checkout.mkdir(exist_ok=False)
        try:
            with cls.acquiring():
                cls.git(checkout.parent, "clone", "--no-checkout",
                        cls.github_url(name), str(checkout), timeout=300)
            return cls.qualified_github(checkout, name)
        except (GitCommandError, ValueError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"Repository acquisition failed at {checkout}; retained for inspection: {error}") from error

    @staticmethod
    def repository_url(url: str) -> RepositoryName:
        """The same Git-origin identity for a checkout and a declared stack source."""
        github = url.removesuffix(".git").replace(":", "/").split("/")[-2:]
        return RepositoryName.from_wire("/".join(github))
