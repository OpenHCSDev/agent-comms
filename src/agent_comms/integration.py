"""Combine reviewed heads and spend the host's test lease once, not once per PR."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, ClassVar

from .active_route import resolve_comms_route
from .build_identity import RepositoryCommit
from .commander.order import CommanderContext
from .child_process import STOP_GRACE_SECONDS, ResidentManager, SpawnedProcess
from .declared_family import DeclaredFamily
from .errors import ToolRefusedError
from .field_codec import FieldCodec, GitCommitText, WireName
from .head_review import BaseSide, HeadReview, HeadSide, IntegrationInput, IntegrationSource, PullRequestHead, Side
from .installer.runtime_resolution import StackSources
from .launch_runtime import CheckoutSource, RestoredRuntime
from .repo import Checkout, DetachedStart, GitCommandError, Repo
from .systemd_unit import AgentsSlice
from .repository_name import RepositoryName
from .test_lease import LeaseStart, TestLease
from .todos import PullRequests
from .work_branches import WorkBranches

class IntegrationFailed(ToolRefusedError):
    pass


@dataclass(frozen=True)
class TestSelection:
    directories: Annotated[tuple[str, ...], WireName("testpaths")]
    patterns: Annotated[tuple[str, ...], WireName("python_files")]

    @classmethod
    def read(cls, checkout: Path) -> TestSelection:
        # Decode only this consumer's declared fields; pytest owns the other options.
        with (checkout / "pyproject.toml").open("rb") as source:
            match tomllib.load(source):
                case {"tool": {"pytest": {"ini_options": {**configuration}}}}:
                    return FieldCodec.decode(
                        cls,
                        {
                            entry.key: configuration[entry.key]
                            for entry in FieldCodec.argument_fields(cls)
                            if entry.key in configuration
                        },
                    )
        raise IntegrationFailed("pyproject.toml requires [tool.pytest.ini_options]")

    def files(self, checkout: Path, changed: tuple[Path, ...]) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    str(path)
                    for path in changed
                    if any(path.is_relative_to(root) for root in self.directories)
                    and any(path.match(pattern) for pattern in self.patterns)
                    and (checkout / path).is_file()
                }
            )
        )


class FailureAction(ABC):
    def require_qualification(self) -> None:
        """Checking source capture does not run or retry a test selection."""

    def require_heads(self, heads: tuple[PullRequestHead, ...]) -> None:
        pass

    @abstractmethod
    def remaining(
        self, heads: tuple[PullRequestHead, ...], log: Path
    ) -> tuple[PullRequestHead, ...]: ...


@dataclass(frozen=True)
class StopAfterFailure(FailureAction):
    def remaining(
        self, heads: tuple[PullRequestHead, ...], log: Path
    ) -> tuple[PullRequestHead, ...]:
        raise IntegrationFailed(
            f"Combined tests failed; {log}. No head is blamed from a test filename. "
            f"A source-supported exclusion must be named with {ExcludeAfterFailure.option}."
        )


@dataclass(frozen=True)
class ExcludeAfterFailure(FailureAction):
    pr: int
    option: ClassVar[str] = "--drop-on-failure"

    @classmethod
    def argument(cls, value: str) -> ExcludeAfterFailure:
        pr = int(value)
        if pr <= 0:
            raise argparse.ArgumentTypeError("A positive pull request number is required")
        return cls(pr)

    def require_qualification(self) -> None:
        raise IntegrationFailed("Qualification cannot exclude a captured source after failure")

    def require_heads(self, heads: tuple[PullRequestHead, ...]) -> None:
        if not any(head.pr == self.pr for head in heads):
            raise IntegrationFailed(f"#{self.pr} is not in the reviewed integration heads")

    def remaining(
        self, heads: tuple[PullRequestHead, ...], log: Path
    ) -> tuple[PullRequestHead, ...]:
        self.require_heads(heads)
        remaining = tuple(head for head in heads if head.pr != self.pr)
        if not remaining:
            raise IntegrationFailed(f"{log}: excluding #{self.pr} leaves no heads to qualify")
        print(f"EXCLUDED #{self.pr} by operator selection after failure: {log}", flush=True)
        return remaining


class IntegrationCheckout(Checkout):
    """The existing checkout owner owns combination and its changed-file selection."""

    def require_editable(self) -> None:
        if not (self.path / ".git").is_file():
            raise IntegrationFailed("Integration requires an isolated Git worktree, not main")
        if self.path == Path(__file__).resolve().parents[2]:
            raise IntegrationFailed("The integration tree cannot replace its running source")
        actual = Path(Repo.git(self.path, "rev-parse", "--show-toplevel").strip()).resolve()
        if actual != self.path:
            raise IntegrationFailed(f"Git selected {actual}, not the requested {self.path}")
        if held := self.held_work():
            raise IntegrationFailed(f"Integration preserves this dirty checkout:\n{held}")

    def fresh_main(self) -> str:
        self.require_editable()
        Repo.git(self.path, "fetch", "origin", timeout=300)
        return Repo.git(self.path, "rev-parse", "origin/main").strip()

    @classmethod
    def create(cls, repository: Repo, path: Path, commit: str) -> IntegrationCheckout:
        if path.exists():
            raise IntegrationFailed(f"Preserve existing qualification checkout: {path}")
        WorkBranches(repository.checkout).require_commits("origin", commit)
        path.parent.mkdir(parents=True, exist_ok=True)
        created = cls.add(repository.checkout, path, DetachedStart(commit))
        created.require_editable()
        return created

    def include(self, head: PullRequestHead) -> None:
        """Fetch exactly the captured member; Git, not a patch heuristic, combines it."""
        Repo.git(self.path, "fetch", "origin", f"refs/pull/{head.pr}/head", timeout=300)
        fetched = Repo.git(self.path, "rev-parse", "FETCH_HEAD").strip()
        if fetched != head.head:
            raise IntegrationFailed(f"#{head.pr} moved while fetching; capture its new head")
        try:
            Repo.git(self.path, "merge", "--no-ff", "--no-edit", head.head, timeout=300)
        except GitCommandError:
            if Repo.answers(self.path, "rev-parse", "-q", "--verify", "MERGE_HEAD"):
                Repo.git(self.path, "merge", "--abort")
            raise

    def combine_sources(self, base: str, heads: tuple[PullRequestHead, ...]) -> str:
        """Checking admits incomplete sources, but never drops a conflicting member."""
        self.require_editable()
        Repo.git(self.path, "switch", "--detach", base)
        for head in heads:
            self.include(head)
        self.prepare()
        return Repo.git(self.path, "rev-parse", "HEAD^{tree}").strip()

    def combine(self, base: str, heads: tuple[PullRequestHead, ...]) -> tuple[PullRequestHead, ...]:
        self.require_editable()
        Repo.git(self.path, "switch", "--detach", base)
        combined = []
        for head in heads:
            current = head.current()
            if not HeadReview.accepted(current.repo, current.head):
                raise IntegrationFailed(f"#{head.pr} no longer has an accepted head review")
            try:
                self.include(current)
            except GitCommandError as error:
                print(f"MERGE REFUSED #{head.pr} {current.head}: {error}", flush=True)
            else:
                combined.append(current)
                print(f"COMBINED #{head.pr} {current.head}", flush=True)
        return tuple(combined)

    def test_files(self, base: str, heads: tuple[PullRequestHead, ...]) -> tuple[str, ...]:
        changed = tuple(
            Path(path)
            for head in heads
            for path in Repo.git(
                self.path, "diff", "--name-only", "-z", "--diff-filter=AMR", f"{base}...{head.head}"
            ).split("\0")
        )
        return TestSelection.read(self.path).files(self.path, changed)


@dataclass(frozen=True)
class TestAttempt:
    log: Path

    @classmethod
    def from_process(cls, result: subprocess.CompletedProcess[bytes], log: Path) -> TestAttempt:
        # The subprocess boundary decodes exit status once into its behavior.
        if result.returncode == 0:
            return PassedTestAttempt(log)
        if result.returncode == TimedOutTestAttempt.exit_code:
            return TimedOutTestAttempt(log)
        return FailedTestAttempt(log, result.returncode)

    @property
    def code(self) -> int:
        return 0

    def finish(
        self, run: IntegrationRun, base: str, heads: tuple[PullRequestHead, ...]
    ) -> int:
        return self.code


class PassedTestAttempt(TestAttempt):
    pass


@dataclass(frozen=True)
class FailedTestAttempt(TestAttempt):
    returncode: int

    @property
    def code(self) -> int:
        return self.returncode

    def finish(
        self, run: IntegrationRun, base: str, heads: tuple[PullRequestHead, ...]
    ) -> int:
        return run.retry(self, base, heads)


class TimedOutTestAttempt(TestAttempt):
    exit_code: ClassVar[int] = 124

    @property
    def code(self) -> int:
        return self.exit_code

    def finish(
        self, run: IntegrationRun, base: str, heads: tuple[PullRequestHead, ...]
    ) -> int:
        raise IntegrationFailed(f"Timed out: {self.log}; no automatic replay")


@dataclass(frozen=True)
class TestedSources(DeclaredFamily, affix="Sources"):
    """The actual tested Git graph and forge carrier, not a supplied source roster.

    Event-specific ancestry and current-main rules live on their event declarations.
    These transient values never enter Core's live records or authorize a review.
    """

    repository: Repo
    tested: str
    carrier: PullRequestHead
    resolve_option: ClassVar[str] = "--resolve-ci-inputs"
    revalidate_option: ClassVar[str] = "--revalidate-ci-inputs"

    @classmethod
    def options(cls) -> tuple[str, ...]:
        return cls.resolve_option, cls.revalidate_option

    @property
    def parents(self) -> tuple[str, ...]:
        return tuple(Repo.git(self.repository.checkout, "rev-list", "--parents", "-n", "1", self.tested).split()[1:])

    @property
    def baseline(self) -> str:
        if len(self.parents) != 2:
            raise IntegrationFailed("The tested comparison requires an actual two-parent merge")
        return self.parents[0]

    @property
    def tree(self) -> str:
        return Repo.git(self.repository.checkout, "rev-parse", f"{self.tested}^{{tree}}").strip()

    @classmethod
    def read(cls, checkout: Path) -> TestedSources:
        repository = Repo.of(checkout)
        if RepositoryName.from_wire(os.environ["GITHUB_REPOSITORY"]) != repository.github:
            raise IntegrationFailed("The runner checkout origin is not the actual event repository")
        tested = Repo.git(checkout, "rev-parse", "HEAD^{commit}").strip()
        if os.environ["GITHUB_SHA"] != tested:
            raise IntegrationFailed("The runner checkout is not the event's actual tested SHA")
        GitCommitText.decode(tested)
        document = FieldCodec.parse(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        return cls.member_named(os.environ["GITHUB_EVENT_NAME"]).resolve(repository, tested, document)

    @classmethod
    @abstractmethod
    def resolve(cls, repository: Repo, tested: str, document: object) -> TestedSources:
        """Decode this foreign event once and prove its relationship to the tested Git graph."""

    def require_carrier(self) -> None:
        current = self.carrier.current()
        if current.base != "main":
            raise IntegrationFailed("The tested carrier no longer targets main")

    def main_commit(self, source: IntegrationSource) -> str:
        return source.baseline.commit

    def materialize(self, directory: Path) -> IntegrationInput:
        """Capture once from Git/forge, then use these same exact checkouts for every consumer."""
        if not directory.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve()):
            raise IntegrationFailed("CI source artifacts belong under the runner's private temporary root")
        directory.mkdir(parents=True, exist_ok=False)
        payload = IntegrationInput.from_message(
            WorkBranches(self.repository.checkout).commit_message(self.carrier.head))
        urls = IntegrationInput.source_urls(self.repository, self.baseline, self.tested)
        repositories = [self.repository]
        for name, url in sorted(urls.items(), key=lambda item: item[0].key):
            if name == self.repository.github:
                raise IntegrationFailed("The stack repeats Core as a Git companion source")
            checkout = directory / "repositories" / Path(*name.key)
            checkout.parent.mkdir(parents=True, exist_ok=True)
            Repo.git(self.repository.checkout, "clone", "--no-checkout", url, str(checkout), timeout=300)
            repository = Repo.of(checkout)
            if repository.github != name:
                raise IntegrationFailed("A declared source clone has a different actual Git origin")
            repositories.append(repository)
        if payload:
            inputs, = payload
        else:
            sources = [IntegrationSource(RepositoryCommit(self.repository.github, self.baseline),
                                         (self.carrier,), self.tree)]
            for repository in repositories[1:]:
                commit = IntegrationSource.main_head(repository)
                WorkBranches(repository.checkout).require_commits("origin", commit)
                tree = Repo.git(repository.checkout, "rev-parse", f"{commit}^{{tree}}").strip()
                sources.append(IntegrationSource(RepositoryCommit(repository.github, commit), (), tree))
            inputs = IntegrationInput(tuple(sorted(sources, key=lambda source: source.repository.key)))
        self.validate(inputs, tuple(repositories))
        for source in inputs.sources:
            repository = Repo.selected(tuple(repositories), source.repository.to_wire())
            IntegrationCheckout.create(repository, BaseSide.checkout(directory, source.repository), source.baseline.commit)
            # The actual tested Core merge already carries all local parents. Fetching its
            # earlier own PR head through refs/pull would instead fetch the new carrier.
            if source.repository == self.repository.github:
                IntegrationCheckout.create(repository, HeadSide.checkout(directory, source.repository), self.tested)
            else:
                head = IntegrationCheckout.create(repository, HeadSide.checkout(directory, source.repository), source.baseline.commit)
                head.combine_sources(source.baseline.commit, source.heads)
            for side in Side.sides():
                side.require_materialized(directory, source)
                checkout = side.checkout(directory, source.repository)
                print(f"SOURCE {side.declared_name} {source.repository.to_wire()} "
                      f"{Repo.git(checkout, 'rev-parse', 'HEAD').strip()} {checkout}", flush=True)
        self.validate(inputs, tuple(repositories))
        inputs.write(directory)
        print(f"CI INPUT {inputs.digest}; source computation only, no review or merge permission", flush=True)
        return inputs

    def revalidate(self, directory: Path) -> IntegrationInput:
        inputs = IntegrationInput.at(directory)
        repositories = []
        for source in inputs.sources:
            for side in Side.sides():
                side.require_materialized(directory, source)
            repositories.append(Repo.of(BaseSide.checkout(directory, source.repository)))
        self.validate(inputs, tuple(repositories))
        print(f"CI INPUT REVALIDATED {inputs.digest}; no verdict posted", flush=True)
        return inputs

    def validate(self, inputs: IntegrationInput, repositories: tuple[Repo, ...]) -> None:
        """Re-run at job exit: source checks compute, but never publish a verdict."""
        self.require_carrier()
        local = inputs.source(self.repository.github)
        if local.baseline.commit != self.baseline or local.candidate_tree != self.tree:
            raise IntegrationFailed("Tested base/tree differs from the frozen source input; recapture")
        inputs.require_repositories(IntegrationInput.declared_repositories(
            self.repository, self.baseline, self.tested))
        payload = IntegrationInput.from_message(
            WorkBranches(self.repository.checkout).commit_message(self.carrier.head))
        if payload:
            if payload != (inputs,):
                raise IntegrationFailed("The tested carrier and materialized input disagree")
            inputs.require_carrier(self.repository, self.carrier)
        else:
            # Ordinary mode is precisely one actual carrier plus unchanged captured mains.
            if local.heads != (self.carrier,) or any(source.heads for source in inputs.sources
                                                    if source.repository != self.repository.github):
                raise IntegrationFailed("An ordinary comparison cannot supply companion overlays")
        for source in inputs.sources:
            repository = Repo.selected(repositories, source.repository.to_wire())
            if IntegrationSource.main_head(repository) != self.main_commit(source):
                raise IntegrationFailed(f"{source.repository.to_wire()} main moved; recapture")
            source.require_candidate(repository, self.carrier)


class PullRequestSources(TestedSources, declared_name="pull_request"):
    @classmethod
    def resolve(cls, repository: Repo, tested: str, document: object) -> TestedSources:
        match document:
            case {"number": int(number), "pull_request": {"head": {"sha": str(head)}}}:
                graph = cls(repository, tested, PullRequestHead(repository.github, number, head))
            case _:
                raise IntegrationFailed("Malformed pull_request source event")
        if graph.parents != (graph.baseline, graph.carrier.head):
            raise IntegrationFailed("The tested second parent is not the event's exact PR head")
        graph.require_carrier()
        return graph


class MergeGroupSources(TestedSources, declared_name="merge_group"):
    @classmethod
    def resolve(cls, repository: Repo, tested: str, document: object) -> TestedSources:
        match document:
            case {"merge_group": {"head_sha": str(head), "base_sha": str(base), "head_ref": str(reference)}}:
                # This is GitHub's queue protocol (also used by review-verdict), not a
                # branch-name guess about which independent repositories belong together.
                queued = re.search(r"/pr-([0-9]+)-", reference)
                if queued is None or head != tested:
                    raise IntegrationFailed("No actual queued carrier in this merge-group event")
                carrier = PullRequestHead.at(repository.github, int(queued[1]))
                graph = cls(repository, tested, PullRequestHead(carrier.repo, carrier.pr, carrier.head))
            case _:
                raise IntegrationFailed("Malformed merge_group source event")
        if graph.baseline != base or not Repo.descends(repository.checkout, graph.carrier.head, tested):
            raise IntegrationFailed("The tested queue graph does not contain its exact current carrier")
        graph.require_carrier()
        return graph


class MainSources(TestedSources, declared_name="push"):
    @staticmethod
    def merged_carrier(repository: Repo, tested: str) -> PullRequestHead:
        response = repository.github.rest(f"commits/{tested}/pulls", "--jq", "map(.number)")
        if response.returncode:
            raise IntegrationFailed(response.stderr.strip() or "Cannot read main's merged PR provenance")
        numbers = FieldCodec.decode(tuple[int, ...], FieldCodec.parse(response.stdout))
        rows = tuple(row for row in PullRequests().rows(repository.github, numbers)
                     if row.base == "main" and any(merged.merge.oid == tested for merged in row.as_merged()))
        if len(rows) != 1:
            raise IntegrationFailed("Main cache requires one actual merged PR; direct/ambiguous commits refuse")
        row, = rows
        return PullRequestHead(repository.github, row.number, row.head)

    @classmethod
    def resolve(cls, repository: Repo, tested: str, document: object) -> TestedSources:
        match document:
            case {"ref": "refs/heads/main", "after": str(after)} if after == tested:
                graph = cls(repository, tested, cls.merged_carrier(repository, tested))
            case _:
                raise IntegrationFailed("Main cache requires the actual main push event")
        if graph.parents != (graph.baseline, graph.carrier.head):
            raise IntegrationFailed("Main cache supports the actual two-parent merged PR, not a guessed cut")
        return graph

    def require_carrier(self) -> None:
        if self.merged_carrier(self.repository, self.tested) != self.carrier:
            raise IntegrationFailed("Main's uniquely identified merged carrier changed")

    def main_commit(self, source: IntegrationSource) -> str:
        if source.repository == self.repository.github:
            return self.tested
        return super().main_commit(source)


@dataclass(frozen=True)
class IntegrationRun:
    checkout: IntegrationCheckout
    output: Path
    risk: str
    failure: FailureAction = field(default_factory=StopAfterFailure)
    pytest_arguments: tuple[str, ...] = ()
    niceness: ClassVar[int] = 19
    timeout: ClassVar[int] = 300

    def __post_init__(self) -> None:
        if not self.risk.strip():
            raise IntegrationFailed("Name the unresolved risk that requires this test run")
        if self.output.is_relative_to(self.checkout.path):
            raise IntegrationFailed("Keep test output outside the source checkout")

    def accepted_heads(self) -> tuple[PullRequestHead, ...]:
        repo = Repo.of(self.checkout.path)
        return tuple(
            PullRequestHead(repo.github, request.number, request.head)
            for row in PullRequests().open(repo.github)
            for request in row.parsed((), ()).as_ready()
            if HeadReview.accepted(repo.github, request.head)
        )

    def qualify(self, context: CommanderContext, problems: tuple[str, ...]) -> IntegrationInput:
        """Prepare one checking carrier from stored orders; never test, post, ready or arm it."""
        self.checkout.require_editable()
        self.failure.require_qualification()
        if not problems or len(set(problems)) != len(problems):
            raise IntegrationFailed("Qualification requires unique actual problem selectors")
        if context.actor_identity.commanding_identity != context.incarnation:
            raise IntegrationFailed("Qualification requires this root's existing commander authority")
        todos = tuple(context.comms.goals.todos.get(problem) for problem in problems)
        for todo in todos:
            todo.require_commander(context.incarnation)
        core = Repo.of(self.checkout.path)
        registered_core = Repo.selected(context.repos, core.github.to_wire())
        base = IntegrationSource.main_head(registered_core)
        WorkBranches(registered_core.checkout).require_commits("origin", base)
        linked_repositories = {link.repo for todo in todos for order in todo.order.declarations
                               for link in order.pull_requests}
        initial = {*IntegrationInput.declared_repositories(registered_core, base, base), *linked_repositories}
        repositories = {name: Repo.selected(context.repos, name.to_wire()) for name in initial}
        mains = {core.github: base, **{name: IntegrationSource.main_head(repo)
                                     for name, repo in repositories.items() if name != core.github}}
        pulls = PullRequests(todos)
        heads: dict[RepositoryName, tuple[PullRequestHead, ...]] = {}
        for repo in repositories.values():
            WorkBranches(repo.checkout).require_commits("origin", mains[repo.github])
            linked = PullRequests.linked_among(todos, repo.github)
            selected = []
            for row in pulls.rows(repo.github, tuple(sorted(linked))):
                if row.as_merged():
                    merged, = row.as_merged()
                    if not repo.contains(merged.merge.oid, mains[repo.github]):
                        raise IntegrationFailed("A merged source is absent from captured main")
                    continue
                if not row.as_unmerged() or len(linked[row.number]) != 1 or row.base != "main":
                    raise IntegrationFailed("A linked source is closed, ambiguous or targets another base")
                selected.append(PullRequestHead(repo.github, row.number, row.head))
            heads[repo.github] = tuple(selected)
        primary = PullRequests.linked_among((todos[0],), core.github)
        carriers = tuple(head for head in heads[core.github] if head.pr in primary)
        if len(carriers) != 1:
            raise IntegrationFailed("The primary problem has no unique existing local carrier")
        self.output.mkdir(parents=True, exist_ok=False)
        IntegrationCheckout.create(registered_core, BaseSide.checkout(self.output, core.github), base)
        tree = self.checkout.combine_sources(base, heads[core.github])
        candidate_commit = Repo.git(self.checkout.path, "rev-parse", "HEAD").strip()
        required = IntegrationInput.declared_repositories(registered_core, base, candidate_commit)
        if linked_repositories - set(required):
            raise IntegrationFailed("A selected order links an unknown stack repository")
        sources = [IntegrationSource(RepositoryCommit(core.github, base), heads[core.github], tree)]
        for name in required:
            if name == core.github:
                continue
            if name not in repositories:
                repositories[name] = Repo.selected(context.repos, name.to_wire())
                mains[name] = IntegrationSource.main_head(repositories[name])
                heads[name] = ()
            repo = repositories[name]
            if len(heads[name]) > 1:
                raise IntegrationFailed("A companion repository has no unique existing linked carrier")
            baseline = mains[name]
            IntegrationCheckout.create(repo, BaseSide.checkout(self.output, name), baseline)
            candidate = IntegrationCheckout.create(repo, HeadSide.checkout(self.output, name), baseline)
            tree = candidate.combine_sources(baseline, heads[name])
            sources.append(IntegrationSource(RepositoryCommit(name, baseline), heads[name], tree))
        # Revalidate original sources before freezing the shared value or attaching its payload.
        for source in sources:
            repo = repositories[source.repository]
            if IntegrationSource.main_head(repo) != source.baseline.commit:
                raise IntegrationFailed("A captured main moved; preserve this failed attempt and recapture")
            for head in source.heads:
                head.current()
            if source.expected_tree(repo) != source.candidate_tree:
                raise IntegrationFailed("Actual and reproduced candidate trees differ")
        current_todos = tuple(context.comms.goals.todos.get(todo.id) for todo in todos)
        for todo in current_todos:
            todo.require_commander(context.incarnation)
        for source in sources:
            linked = PullRequests.linked_among(current_todos, source.repository)
            if any(head.pr not in linked for head in source.heads):
                raise IntegrationFailed("A selected problem no longer links its captured source")
        inputs = IntegrationInput(tuple(sorted(sources, key=lambda source: source.repository.key)))
        context.comms.goals.todos.require_input_links(inputs)
        if Repo.git(self.checkout.path, "rev-parse", f"{base}^{{tree}}").strip() == inputs.source(core.github).candidate_tree:
            raise IntegrationFailed("The Core carrier has no substantive source change")
        original_message = WorkBranches(self.checkout.path).commit_message(candidate_commit)
        if IntegrationInput.from_message(original_message):
            raise IntegrationFailed("Capture requires a fresh substantive integration commit, not nested metadata")
        Repo.git(self.checkout.path, "commit", "--amend", "-m",
                 f"{original_message.rstrip()}\n\n{inputs.trailer}")
        commit = Repo.git(self.checkout.path, "rev-parse", "HEAD").strip()
        IntegrationCheckout.create(registered_core, HeadSide.checkout(self.output, core.github), commit)
        inputs.write(self.output)
        print(f"QUALIFICATION ONLY {commit}; inputs {inputs.digest}; {self.output}; no review or merge permission", flush=True)
        return inputs

    def test(
        self, base: str, heads: tuple[PullRequestHead, ...], start: LeaseStart, attempt: int
    ) -> TestAttempt:
        selected = self.checkout.test_files(base, heads)
        if not selected:
            raise IntegrationFailed("The touched-test union is empty; no suite fallback was run")
        self.checkout.prepare()
        log = self.output / f"attempt-{attempt}.log"
        command = (
            "timeout",
            "-s",
            "INT",
            str(self.timeout),
            sys.executable,
            "-m",
            "pytest",
            *self.pytest_arguments,
            "-o",
            "faulthandler_timeout=60",
            "--basetemp",
            str(self.output / f"pytest-{attempt}"),
            *start.arguments(),
            *selected,
        )
        print(
            f"RUN {attempt}: {len(selected)} test files; risk: {self.risk}; output: {log}",
            flush=True,
        )
        runtime = RestoredRuntime(Path(sys.executable))
        environment = CheckoutSource(self.checkout.path / "src").environment(
            runtime.environment(os.environ)
        )
        with log.open("xb") as output:
            tree = Repo.git(self.checkout.path, "rev-parse", "HEAD").strip()
            output.write(f"Base: {base}\nTree: {tree}\nRisk: {self.risk}\n".encode())
            for head in heads:
                output.write(f"Head: #{head.pr} {head.head}\n".encode())
            output.flush()
            with SpawnedProcess.launch(
                command,
                cwd=self.checkout.path,
                env=environment,
                output=output,
                stderr=subprocess.STDOUT,
                pass_fds=start.descriptors(),
                runtime=runtime,
            ) as child:
                try:
                    result = child.collect(timeout=self.timeout + STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    answer: TestAttempt = TimedOutTestAttempt(log)
                else:
                    answer = TestAttempt.from_process(result, log)
        for head in heads:
            print(f"COMBINED EXIT {answer.code}: #{head.pr} {head.head}; {log}", flush=True)
        return answer

    def retry(
        self,
        failed: FailedTestAttempt,
        base: str,
        heads: tuple[PullRequestHead, ...],
    ) -> int:
        remaining = self.failure.remaining(heads, failed.log)
        combined = self.checkout.combine(base, remaining)
        if not combined:
            raise IntegrationFailed("No remainder combined; no tests run")
        with TestLease.current().held() as start:
            return self.test(base, combined, start, 2).code

    def execute(self) -> int:
        self.checkout.require_editable()
        AgentsSlice.require_current()
        os.nice(max(0, self.niceness - os.getpriority(os.PRIO_PROCESS, 0)))
        heads = self.accepted_heads()
        if not heads:
            print("No accepted ready heads; no tests run", flush=True)
            return 0
        self.failure.require_heads(heads)
        self.output.mkdir(parents=True, exist_ok=False)
        base = self.checkout.fresh_main()
        combined = self.checkout.combine(base, heads)
        if not combined:
            raise IntegrationFailed("No heads combined; no tests run")
        with TestLease.current().held() as start:
            answer = self.test(base, combined, start, 1)
        return answer.finish(self, base, combined)


def main() -> None:
    """Enter the existing CPU-slice owner before acquiring a transferable lease."""
    if any(argument.partition("=")[0] in TestedSources.options() for argument in sys.argv[1:]):
        raise SystemExit(run())
    command = ResidentManager.current().command((sys.executable, "-m", __name__, *sys.argv[1:]))
    os.execvpe(command[0], command, dict(os.environ))


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        help="new persistent directory for this run's two bounded logs",
    )
    parser.add_argument("--risk", default="")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(TestedSources.resolve_option, type=Path, metavar="OUTPUT",
                           help="materialize the actual GitHub event's exact compared sources; no bus")
    operation.add_argument(TestedSources.revalidate_option, type=Path, metavar="OUTPUT",
                           help="revalidate the same event/input and source heads before CI success")
    operation.add_argument("--qualify", default="", metavar="PROBLEM",
                           help="prepare sources from this actual primary order, without running tests")
    parser.add_argument("--with-problem", action="append", default=[], metavar="PROBLEM")
    parser.add_argument("--comms-root", type=Path,
                        help="the existing authorized root for stored-problem qualification")
    parser.add_argument(
        ExcludeAfterFailure.option, type=ExcludeAfterFailure.argument, default=StopAfterFailure()
    )
    parser.add_argument(
        "pytest_arguments",
        nargs=argparse.REMAINDER,
        help="pytest's full argument surface after --; touched files are appended",
    )
    arguments = parser.parse_args()
    pytest_arguments = arguments.pytest_arguments
    if pytest_arguments[:1] == ["--"]:
        pytest_arguments = pytest_arguments[1:]
    if arguments.resolve_ci_inputs or arguments.revalidate_ci_inputs:
        if pytest_arguments or arguments.with_problem or arguments.comms_root or arguments.output or arguments.risk:
            parser.error("CI source resolution does not accept tests, a bus root or local qualification options")
        arguments.drop_on_failure.require_qualification()
        graph = TestedSources.read((arguments.checkout or Path.cwd()).resolve())
        if arguments.resolve_ci_inputs:
            graph.materialize(arguments.resolve_ci_inputs.resolve())
        else:
            graph.revalidate(arguments.revalidate_ci_inputs.resolve())
        return 0
    if arguments.checkout is None or arguments.output is None or not arguments.risk:
        parser.error("Local integration requires --checkout, --output and --risk")
    run = IntegrationRun(
        IntegrationCheckout(arguments.checkout.resolve()),
        arguments.output.resolve(),
        arguments.risk,
        arguments.drop_on_failure,
        tuple(pytest_arguments),
    )
    # Decode the foreign CLI operation once; qualification never enters execute/test/lease.
    if arguments.qualify:
        if pytest_arguments:
            parser.error("Qualification cannot run tests")
        context = CommanderContext.read_root(resolve_comms_route(arguments.comms_root).root)
        run.qualify(context, (arguments.qualify, *arguments.with_problem))
        return 0
    if arguments.with_problem:
        parser.error("--with-problem requires --qualify")
    return run.execute()


if __name__ == "__main__":
    raise SystemExit(run())
