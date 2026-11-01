"""Configured storage limits over current filesystem and Git worktrees."""
from __future__ import annotations

import json
import math
import shutil
import time
from abc import abstractmethod
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.child_process import ResidentMemory
from agent_comms.nominal.field_codec import (
    FieldCodec,
    GoalId,
    MalformedRecord,
    ValueDirective,
)
from agent_comms.locked_store import DurableFile
from agent_comms.errors import TodoError
from agent_comms.problem_number import ProblemNumber
from agent_comms.repo import Checkout, GitCommandError, Repo, RepositoryReads
from agent_comms.repository_name import RepositoryName
from agent_comms.storage_room import RoomAllocation
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.work_branches import WorkBranches

from .shell_effects import StorageDenied
from .thread_storage import (
    StoragePath,
    StorageRoots,
)
from .threads import Thread
from .store_files import ExclusiveAtomicFile
from .private_path import PrivateDirectoryRole

if TYPE_CHECKING:
    from agent_comms.registration import Registration
    from agent_comms.todos import Todo, TodoStore











@dataclass(frozen=True)
class MemoryHolder:
    """One temp dir or test process and the memory it holds."""

    name: str
    gib: float

    def __str__(self) -> str:
        return f"{self.name} {self.gib:.1f} GiB"


@dataclass(frozen=True)
class MemoryUse:
    """How much memory is available now and what holds it, read from /proc and the temp root."""

    available_gib: float
    idle_dirs: tuple[MemoryHolder, ...]
    processes: tuple[MemoryHolder, ...]


class CheckoutAllocation(DeclaredFamily, affix="CheckoutAllocation"):
    """A path's declared allocation; each member owns its interpretation and Git needs."""

    @classmethod
    def for_path(cls, rule: StorageRule, owner: Thread,
                 path: Path) -> Iterator[CheckoutAllocation]:
        allocated = rule.roots_of(owner).allocation_name
        prefix = f'{allocated}{rule.checkout_separator}'
        name = (path.name.removeprefix(prefix)
                if path.parent == rule.checkout_base and path.name.startswith(prefix) else '')
        problem, _, suffix = name.partition(rule.checkout_separator)
        for member in cls.members_with(cls):
            yield from member.read(owner, path, problem, suffix)

    @classmethod
    @abstractmethod
    def read(cls, owner: Thread, path: Path, problem: str,
             suffix: str) -> Iterator[CheckoutAllocation]: ...

    @abstractmethod
    def owns_present(self, rule: StorageRule, owner: Thread, path: Path,
                     reads: RepositoryReads) -> bool: ...

    def owns_declared(self, rule: StorageRule, owner: Thread, path: Path,
                      repository: RepositoryName, reads: RepositoryReads) -> bool:
        return self.owns_present(rule, owner, path, reads)

    @classmethod
    def for_work(cls, rule: StorageRule, owner: Thread, problem: str,
                 primary: RepositoryName, companions: tuple[RepositoryName, ...],
                 generation: GoalId) -> tuple[Path, ...]:
        """Read/launch allocations reserve no editable work; declared members supply it."""
        return ()

    @classmethod
    def source_repositories(cls, primary: RepositoryName,
                            companions: tuple[RepositoryName, ...]) -> tuple[RepositoryName, ...]:
        return ()

    def editable_roots(self, rule: StorageRule, owner: Thread, store: TodoStore,
                       repository: RepositoryName) -> frozenset[Path]:
        return frozenset()

    def reserves_boundary(self) -> bool:
        return False

    def edit_boundary(self, rule: StorageRule, owner: Thread, path: Path,
                      store: TodoStore, reads: RepositoryReads) -> frozenset[Path]:
        return frozenset()


@dataclass(frozen=True)
class HomeCheckoutAllocation(CheckoutAllocation):
    @classmethod
    def read(cls, owner: Thread, path: Path, problem: str,
             suffix: str) -> Iterator[CheckoutAllocation]:
        if path == owner.project_path:
            yield cls()

    def owns_present(self, rule: StorageRule, owner: Thread, path: Path,
                     reads: RepositoryReads) -> bool:
        return path in owner.home_checkouts


class EditableCheckoutAllocation(CheckoutAllocation):
    """Declared editable members acquire Git identity once at the source-write boundary."""

    def reserves_boundary(self) -> bool:
        return True

    def edit_boundary(self, rule: StorageRule, owner: Thread, path: Path,
                      store: TodoStore, reads: RepositoryReads) -> frozenset[Path]:
        try:
            repository = reads.repository(path).github
            if not self.owns_declared(rule, owner, path, repository, reads):
                return frozenset()
            return frozenset(root for root in self.editable_roots(rule, owner, store, repository)
                             if root == path)
        except (GitCommandError, TodoError) as error:
            raise StorageDenied(f'Cannot authorize source write in {path}: {error}') from error


class ProblemCheckoutAllocation(EditableCheckoutAllocation):
    """A checkout allocated to one problem; ``verb`` is the command that creates it."""

    verb: ClassVar[str]
    problem: str

    def editable_roots(self, rule: StorageRule, owner: Thread, store: TodoStore,
                       repository: RepositoryName) -> frozenset[Path]:
        current = store.get(self.problem)
        current.state.require_open(current)
        assignment = current.held()
        current.require_assignee(owner.incarnation, assignment.generation)
        companions = current.order.source_repositories()
        if repository not in self.source_repositories(current.repo, companions):
            return frozenset()
        return rule.edit_boundaries(owner, current.id, current.repo, companions, assignment.generation)

    @classmethod
    def source_repositories(cls, primary: RepositoryName,
                            companions: tuple[RepositoryName, ...]) -> tuple[RepositoryName, ...]:
        return (primary,)

    @classmethod
    def read(cls, owner: Thread, path: Path, problem: str,
             suffix: str) -> Iterator[CheckoutAllocation]:
        if ProblemNumber.accepts(problem):
            yield from cls.allocated(problem, suffix)

    @classmethod
    @abstractmethod
    def allocated(cls, problem: str, suffix: str) -> Iterator[CheckoutAllocation]: ...


@dataclass(frozen=True)
class OrderCheckoutAllocation(ProblemCheckoutAllocation):
    verb: ClassVar[str] = 'checkout'
    problem: str

    @classmethod
    def allocated(cls, problem: str, suffix: str) -> Iterator[CheckoutAllocation]:
        if not suffix:
            yield cls(problem)

    @classmethod
    def for_work(cls, rule: StorageRule, owner: Thread, problem: str,
                 primary: RepositoryName, companions: tuple[RepositoryName, ...],
                 generation: GoalId) -> tuple[Path, ...]:
        return (rule.order_checkout(rule.roots_of(owner), problem),)

    def owns_present(self, rule: StorageRule, owner: Thread, path: Path,
                     reads: RepositoryReads) -> bool:
        return path == rule.order_checkout(rule.roots_of(owner), self.problem)


class RepositoryAllocation(CheckoutAllocation):
    """A repository-suffixed allocation reads its origin once to establish ownership."""

    suffix: str

    @abstractmethod
    def owns_declared(self, rule: StorageRule, owner: Thread, path: Path,
                      repository: RepositoryName, reads: RepositoryReads) -> bool: ...

    def owns_present(self, rule: StorageRule, owner: Thread, path: Path,
                     reads: RepositoryReads) -> bool:
        try:
            repository = reads.repository(path).github
        except GitCommandError as error:
            raise StorageDenied(
                f'Cannot read repository {self.suffix} of checkout {path}: {error}') from error
        return self.owns_declared(rule, owner, path, repository, reads)


@dataclass(frozen=True)
class RepositoryCheckoutAllocation(ProblemCheckoutAllocation, RepositoryAllocation):
    verb: ClassVar[str] = 'repository-checkout'
    problem: str
    suffix: str

    @classmethod
    def for_work(cls, rule: StorageRule, owner: Thread, problem: str,
                 primary: RepositoryName, companions: tuple[RepositoryName, ...],
                 generation: GoalId) -> tuple[Path, ...]:
        return tuple(rule.repository_checkout(rule.roots_of(owner), problem, repository)
                     for repository in companions)

    @classmethod
    def source_repositories(cls, primary: RepositoryName,
                            companions: tuple[RepositoryName, ...]) -> tuple[RepositoryName, ...]:
        return companions

    @classmethod
    def allocated(cls, problem: str, suffix: str) -> Iterator[CheckoutAllocation]:
        if suffix:
            yield cls(problem, suffix)

    def owns_declared(self, rule: StorageRule, owner: Thread, path: Path,
                      repository: RepositoryName, reads: RepositoryReads) -> bool:
        return path == rule.repository_checkout(rule.roots_of(owner), self.problem, repository)


@dataclass(frozen=True)
class GoalCheckoutAllocation(RepositoryAllocation, EditableCheckoutAllocation):
    goal: GoalId
    suffix: str

    @classmethod
    def source_repositories(cls, primary: RepositoryName,
                            companions: tuple[RepositoryName, ...]) -> tuple[RepositoryName, ...]:
        return tuple(dict.fromkeys((primary, *companions)))

    def editable_roots(self, rule: StorageRule, owner: Thread, store: TodoStore,
                       repository: RepositoryName) -> frozenset[Path]:
        roots = frozenset(path.resolve() for goal in owner.goal.matching(self.goal)
                          for path in self.for_goal(rule, owner, goal.id, repository))
        for current in store.for_goal(owner.incarnation, self.goal):
            companions = current.order.source_repositories()
            if repository in (current.repo, *companions):
                roots |= rule.edit_boundaries(owner, current.id, current.repo, companions, self.goal)
        return roots

    @classmethod
    def for_work(cls, rule: StorageRule, owner: Thread, problem: str,
                 primary: RepositoryName, companions: tuple[RepositoryName, ...],
                 generation: GoalId) -> tuple[Path, ...]:
        return tuple(path for repository in cls.source_repositories(primary, companions)
                     for path in cls.for_goal(rule, owner, generation, repository))

    @classmethod
    def for_goal(cls, rule: StorageRule, owner: Thread, generation: GoalId,
                 repository: RepositoryName) -> tuple[Path, ...]:
        return (rule.repository_checkout(rule.roots_of(owner), generation, repository),)

    @classmethod
    def read(cls, owner: Thread, path: Path, problem: str,
             suffix: str) -> Iterator[CheckoutAllocation]:
        # An allocated checkout can also be the current project. Its Goal identity,
        # not the fact it is home, determines whether it grants editable work.
        if not suffix:
            return
        try:
            goal = FieldCodec.decode(GoalId, problem)
        except MalformedRecord:
            return  # Another allocation member owns names outside the goal-id spelling.
        yield cls(goal, suffix)

    def owns_declared(self, rule: StorageRule, owner: Thread, path: Path,
                      repository: RepositoryName, reads: RepositoryReads) -> bool:
        return path == rule.repository_checkout(rule.roots_of(owner), self.goal, repository)


class ScopedDirectory(ValueDirective[Path]):
    """A declared relative directory whose physical target must remain below its root."""

    def within(self, root: Path) -> Path:
        path = root / self.value
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Storage directory {path} escapes its root {root}")
        return path


@dataclass(frozen=True)
class StorageRule(RoomAllocation, DurableFile):
    filename: ClassVar[str] = "storage.json"
    # Shared Python control host, measured through real three/29-owner journeys.
    host_memory_bytes: ClassVar[int] = 512 * 2**20
    # Independent interactive children; a reporting budget, never a killing limit.
    native_owner_memory_bytes: ClassVar[int] = 192 * 2**20
    floor_gib: float = 10.0  # under this nothing that allocates may run
    cleanup_gib: float = 30.0  # under this a cleanup owner is due; it does not stop builds
    memory_gib: float = 3.0  # available memory under this makes a cleanup owner due
    idle_minutes: float = 30.0  # a pytest temp dir untouched this long holds memory for nothing
    temp_root: Annotated[StoragePath, ScopedDirectory(Path('tmp'))] = Path('/tmp')
    proc: StoragePath = Path('/proc')
    shown: ClassVar[int] = 5  # holders named in each list
    # Defaults use the current home; each declaration also owns its private-root location.
    volume: Annotated[StoragePath, ScopedDirectory(Path())] = field(default_factory=Path.home)
    scratch_base: Annotated[StoragePath, ScopedDirectory(Path('scratch'))] = field(
        default_factory=lambda: Path.home() / '.cache' / 'agent-scratch')
    checkout_base: Annotated[StoragePath, ScopedDirectory(Path('checkouts'))] = field(
        default_factory=lambda: Path.home() / 'wt')
    # Joins a checkout's allocation, problem and companion repository: one name, one format.
    checkout_separator: ClassVar[str] = '--'

    def __post_init__(self):
        limits = (self.floor_gib, self.cleanup_gib)
        if not all(math.isfinite(n) for n in limits):
            raise ValueError("Storage limits must be finite.")
        if min(limits) < 0:
            raise ValueError("Storage limits cannot be negative.")
        # The declared root is canonical; allocation consumers never resolve it again.
        object.__setattr__(self, "checkout_base", self.checkout_base.expanduser().resolve())

    @classmethod
    def load(cls, root: Path) -> Self:
        path = cls.path_in(root)
        return FieldCodec.decode(cls, json.loads(path.read_text()) if path.exists() else {})

    def within(self, root: Path) -> Self:
        """Keep allocation and disposable output inside a verification root, retaining limits."""
        root = root.resolve()
        return replace(self, **{
            plan.name: directory.within(root)
            for plan in FieldCodec.field_plans(type(self))
            for directory in ScopedDirectory.single(plan.annotation)
        })

    def require_isolated(self, root: Path) -> None:
        """Observe private scope without writing or accepting an escaping symlink."""
        if self != self.within(root):
            raise ValueError(f"Existing storage policy escapes verification root {root}")

    def prepare_isolated(self, root: Path) -> Self:
        """Persist private allocation before registration loads it; never rewrite an old policy."""
        scoped = self.within(root)
        PrivateDirectoryRole.make_parents(root)
        try:
            ExclusiveAtomicFile(self.path_in(root)).write_text(
                FieldCodec.compact(FieldCodec.encode(scoped)))
        except FileExistsError:
            stored = self.load(root)
            stored.require_isolated(root)
            return stored
        return scoped

    def initial_roots(self, name: str) -> StorageRoots:
        return StorageRoots(ScopedDirectory(Path(name)).within(self.scratch_base))

    def roots_of(self, thread: Thread) -> StorageRoots:
        return thread.storage_roots.assigned(self, thread.name)

    def order_checkout(self, roots: StorageRoots, problem: str) -> Path:
        """Allocation path for a new problem checkout of its order's check repository; an
        existing source may be reused."""
        return self.checkout_base / f'{roots.allocation_name}{self.checkout_separator}{problem}'

    def allocation_names(self, boundary: Path) -> Iterator[str]:
        """order_checkout's inverse: every allocation name whose order checkouts could be named
        ``boundary``, one per separator position in its name."""
        if boundary.parent != self.checkout_base:
            return
        name = boundary.name
        split = name.find(self.checkout_separator)
        while split != -1:
            yield name[:split]
            split = name.find(self.checkout_separator, split + 1)

    def repository_checkout(self, roots: StorageRoots, problem: str,
                            repository: RepositoryName) -> Path:
        """The same problem's checkout of another repository, changed in lockstep with it."""
        checkout = self.order_checkout(roots, problem)
        _, name = repository.key
        return checkout.with_name(f'{checkout.name}{self.checkout_separator}{name}')

    def edit_boundaries(self, owner: Thread, problem: str, primary: RepositoryName,
                        companions: tuple[RepositoryName, ...], generation: GoalId) -> frozenset[Path]:
        """Physical editable roots follow declared allocations, even before materialization."""
        return frozenset(path.resolve()
                         for member in CheckoutAllocation.members_with(CheckoutAllocation)
                         for path in member.for_work(self, owner, problem, primary, companions, generation))

    def problem_checkouts(self, roots: StorageRoots, problem: str) -> tuple[Path, ...]:
        """Every checkout allocated to one problem: its check repository's, then each other's."""
        checkout = self.order_checkout(roots, problem)
        return (checkout, *sorted(checkout.parent.glob(f'{checkout.name}{self.checkout_separator}*')))

    def work_for_problem(
        self, problem: Todo, registry: Registration, todos: TodoStore,
    ) -> tuple[OrderCheckoutWork, ...]:
        """Find retained work, including a checkout of another problem linked to this order."""
        related = todos.related_problems(problem)
        found = []
        for owner in registry.all_threads().values():
            paths = tuple(path for row in related
                          for path in self.problem_checkouts(self.roots_of(owner), row.id))
            present = []
            for path in paths:
                try:
                    path.stat()
                except FileNotFoundError:
                    continue  # An absent allocation holds no files; other errors propagate.
                present.append(path)
            if not present:
                continue
            storage = UnitStorage.read(self, owner, RepositoryReads())
            for path in present:
                if not storage.owns(Checkout(path)):
                    self.deny(f'{path} is not a registered Git checkout of {owner.name}')
                work = OrderCheckoutWork(path, owner.incarnation)
                if work.read_checkout().holds_work:
                    found.append(work)
        return tuple(found)

    def owns_checkout(self, owner: Thread, path: Path,
                      repository: RepositoryName, reads: RepositoryReads) -> bool:
        """Ownership follows the declared repository allocation, before its checkout exists."""
        return any(allocation.owns_declared(self, owner, path, repository, reads)
                   for allocation in CheckoutAllocation.for_path(self, owner, path))

    def owns_present_checkout(self, owner: Thread, path: Path, reads: RepositoryReads) -> bool:
        """Only a repository-suffixed allocation needs this pass's captured origin."""
        return any(allocation.owns_present(self, owner, path, reads)
                   for allocation in CheckoutAllocation.for_path(self, owner, path))

    def require_completed_transfer(self, owner: Thread, source: Path, destination: Path,
                                   threads: Iterable[Thread], repository: Repo) -> None:
        """An absent old path may identify only this caller's current physical allocation."""
        prefix = self.order_checkout(self.roots_of(owner), '').name
        if source.parent != self.checkout_base or not source.name.startswith(prefix):
            self.deny(f'Missing source {source} is outside {owner.name}\'s allocations')
        self.require_transfer_source(owner, destination, threads, repository)

    def require_transfer_source(self, owner: Thread, source: Path,
                                threads: Iterable[Thread], repository: Repo) -> None:
        """Relocation ownership is not permission to edit the old order."""
        reads = RepositoryReads()
        if source.is_symlink() or source != source.resolve() or source.parent != self.checkout_base:
            self.deny(f'{source} is not a physical checkout in {self.checkout_base}')
        if source == repository.checkout.resolve():
            self.deny(f'{source} is the registered repository checkout, not an isolated allocation')
        if not (self.owns_present_checkout(owner, source, reads) or owner.ran_in(Checkout(source))):
            self.deny(f'{source} is not a current or historical checkout of {owner.name}')
        owners = tuple(thread for thread in threads
                       if thread.incarnation != owner.incarnation
                       and self.owns_present_checkout(thread, source, reads))
        if owners:
            self.deny(f'{source} has foreign or ambiguous checkout owners: '
                      + ', '.join(thread.name for thread in owners))
        if reads.repository(source).github != repository.github:
            self.deny(f'{source} is not a checkout of {repository.github}')
        if not (source / '.git').is_file():
            self.deny(f'{source} is a shared main/home checkout, not a linked worktree')

    def checkout_paths(self, owner: Thread, reads: RepositoryReads) -> tuple[Path, ...]:
        """Declared home and work allocations; Git metadata supplies their branches."""
        ordered = self.order_checkout(self.roots_of(owner), '*')
        return tuple(path for path in (owner.project_path,
                                       *sorted(ordered.parent.glob(ordered.name)))
                     if path.is_dir() and self.owns_present_checkout(owner, path, reads))

    def checkouts_of(self, thread: Thread, reads: RepositoryReads) -> tuple[Path, ...]:
        """Read each repository through the caller's cut; directories and names do not register
        ownership."""
        home = thread.project_path
        candidates = (home, *self.checkout_paths(thread, reads))
        checkouts = {home}
        listed: set[Path] = set()
        for repository in Checkout.git_roots(*candidates):
            if repository in listed:
                continue
            entries = WorkBranches(repository, reads=reads).read_checkout().worktrees()
            listed.update(entries)
            checkouts.update(path for path in entries
                             if path.is_dir() and self.owns_present_checkout(thread, path, reads))
        return (home, *sorted(checkouts - {home}))

    def deny(self, reason):
        raise StorageDenied(f'StorageRule: {reason}')

    def free_gib(self):
        return shutil.disk_usage(self.volume).free / 2**30

    def memory(self) -> MemoryUse:
        """Available memory, the largest idle pytest temp dirs and the largest test processes."""
        available = self.memory_available_bytes()
        idle = time.time() - self.idle_minutes * 60
        dirs = (MemoryHolder(str(path), self.tree_gib(path))
                for path in self.temp_root.glob('pytest-of-*/*')
                if path.is_dir() and path.stat().st_mtime < idle)
        return MemoryUse(available / 2**30, self.largest(dirs),
                         self.largest(self.test_processes()))

    def memory_available_bytes(self):
        lines = (self.proc / 'meminfo').read_text().splitlines()
        return next(int(line.split()[1]) * 1024 for line in lines
                    if line.startswith('MemAvailable:'))

    def largest(self, holders: Iterator[MemoryHolder]) -> tuple[MemoryHolder, ...]:
        return tuple(sorted(holders, key=lambda holder: -holder.gib)[:self.shown])

    @staticmethod
    def tree_gib(path: Path) -> float:
        return sum(map(StorageRule.held_bytes, path.rglob('*'))) / 2**30

    @staticmethod
    def held_bytes(item: Path) -> int:
        try:
            return item.lstat().st_size
        except FileNotFoundError:
            return 0  # removed while read (old temp dirs are pruned), so it holds nothing

    def test_processes(self) -> Iterator[MemoryHolder]:
        """Each live pytest process by resident size; one that exits while read is not listed."""
        for pid in self.proc.glob('[0-9]*'):
            try:
                argv = (pid / 'cmdline').read_bytes().split(b'\0')
                memory = ResidentMemory.read(pid)
            except (FileNotFoundError, ProcessLookupError):
                continue
            if memory.resident and any(b'pytest' in word for word in argv):
                yield MemoryHolder(f'pid {pid.name}', memory.resident / 2**30)

    def memory_due(self, memory: MemoryUse) -> bool:
        return memory.available_gib < self.memory_gib

    def host_memory_exceeded(self, memory: ResidentMemory) -> bool:
        return max(memory.resident, memory.peak) > self.host_memory_bytes

    def fleet_memory_budget(self, owners: int) -> int:
        return self.host_memory_bytes + owners * self.native_owner_memory_bytes

    def below_floor(self, free_gib: float) -> bool:
        return free_gib < self.floor_gib

    def cleanup_due(self, free_gib: float) -> bool:
        return free_gib < self.cleanup_gib

    def require_floor(self) -> None:
        if self.below_floor(self.free_gib()):
            self.deny(f'allocation requires {self.floor_gib:g} GiB free on {self.volume}')

    def require_destination(self, thread: Thread, path: Path) -> None:
        """Every destination obeys artifact and scratch ownership, without reading Git."""
        if '.artifacts' in path.parts:
            self.deny('writes inside .artifacts are forbidden')
        scratch = self.roots_of(thread).scratch
        if path.is_relative_to(self.scratch_base) and not path.is_relative_to(scratch):
            self.deny(f'foreign scratch destination {path}; registered scratch is {scratch}')

    def order_text(self, thread: Thread, pythons: Sequence[str]) -> str:
        """The rule uses the same declared paths as storage enforcement."""
        checkout = self.order_checkout(self.roots_of(thread), "P<n>")
        return (
            f'Storage (refused without a strike): your project is {thread.project_path}; '
            f'your checkouts are '
            f'{checkout} (another repository changed in the '
            f'same problem: {checkout}{self.checkout_separator}<repository name>), '
            f'created with agent-comms {OrderCheckoutAllocation.verb} --from {thread.name} add PROBLEM '
            f'(another repository: agent-comms {RepositoryCheckoutAllocation.verb} '
            f'--from {thread.name} add PROBLEM OWNER/REPO), '
            f'removed with agent-comms {OrderCheckoutAllocation.verb} '
            f'--from {thread.name} remove PROBLEM in the '
            'same order that created them; '
            'your only scratch dir is '
            f'{self.roots_of(thread).scratch}/. '
            f'Run tests with PYTHONPATH=src on the shared {" or ".join(pythons)}; '
            'never create a venv, never uv sync, never write .artifacts, never git clone. '
            f'Below {self.floor_gib:g} GB free on {self.volume}, no worktree, venv, build, install '
            'or recording.'
        )


@dataclass(frozen=True)
class OrderCheckoutWork(WorkBranches):
    """An order checkout records its owner's identity, not its unrelated live state."""

    owner: ThreadIncarnation


@dataclass(frozen=True)
class UnitStorage:
    """Declared checkout membership, independent of the bytes already held."""

    rule: StorageRule
    unit: ThreadIncarnation
    roots: StorageRoots
    home: Checkout
    extra_checkouts: tuple[Checkout, ...]

    @classmethod
    def read(cls, rule: StorageRule, thread: Thread, reads: RepositoryReads) -> UnitStorage:
        home, *extras = rule.checkouts_of(thread, reads)
        return cls(rule, thread.incarnation, rule.roots_of(thread), Checkout(home),
                   tuple(Checkout(path) for path in extras))

    def owns(self, checkout: Checkout) -> bool:
        return checkout == self.home or checkout in self.extra_checkouts

    @property
    def missing_home(self) -> bool:
        return not self.home.path.is_dir()

    def adding(self, checkout: Checkout) -> UnitStorage:
        if self.owns(checkout):
            return self
        return replace(self, extra_checkouts=(*self.extra_checkouts, checkout))

    def removing(self, checkout: Checkout) -> UnitStorage:
        if checkout not in self.extra_checkouts:
            self.rule.deny('only a current extra checkout can be removed')
        return replace(self, extra_checkouts=tuple(
            current for current in self.extra_checkouts if current != checkout))

    def require_retired(self, problem: str) -> None:
        for path in self.rule.problem_checkouts(self.roots, problem):
            if Checkout(path) in self.extra_checkouts:
                self.rule.deny(
                    f'{self.unit.name} still has the checkout {path} for this order; '
                    'it removes it before the order counts as done')
