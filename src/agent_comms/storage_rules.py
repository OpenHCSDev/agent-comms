"""Configured storage limits over current filesystem and Git worktrees."""
from __future__ import annotations

import json
import math
import shutil
import time
from abc import abstractmethod
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from subprocess import TimeoutExpired
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.declared_family import DeclaredFamily
from agent_comms.child_process import ResidentMemory
from agent_comms.field_codec import (
    FieldCodec,
    GoalId,
    MalformedRecord,
)
from agent_comms.locked_store import DurableFile
from agent_comms.errors import TodoConflict, TodoError
from agent_comms.problem_number import ProblemNumber
from agent_comms.repo import Checkout, GitCommandError, Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.storage_room import RoomAllocation
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.work_branches import WorkBranchError, WorkBranches

from .shell_effects import StorageDenied, StorageEffect
from .thread_storage import (
    StoragePath,
    StorageRoots,
)
from .threads import Thread

if TYPE_CHECKING:
    from agent_comms.registration import Registration
    from agent_comms.comms import Comms
    from agent_comms.registry_document import RegistrySnapshot
    from agent_comms.todos import ProgressRead, Todo, TodoStore
    from agent_comms.held_orders import LiveHold

    from .native_tool_call import CodingCall











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
        prefix = f'{allocated}--'
        name = (path.name.removeprefix(prefix)
                if path.parent == rule.checkout_base and path.name.startswith(prefix) else '')
        problem, _, suffix = name.partition('--')
        for member in cls.members_with(cls):
            yield from member.read(owner, path, problem, suffix)

    @classmethod
    @abstractmethod
    def read(cls, owner: Thread, path: Path, problem: str,
             suffix: str) -> Iterator[CheckoutAllocation]: ...

    @abstractmethod
    def owns_present(self, rule: StorageRule, owner: Thread, path: Path) -> bool: ...

    def owns_declared(self, rule: StorageRule, owner: Thread, path: Path,
                      repository: RepositoryName) -> bool:
        return self.owns_present(rule, owner, path)

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
                      store: TodoStore) -> frozenset[Path]:
        return frozenset()


@dataclass(frozen=True)
class HomeCheckoutAllocation(CheckoutAllocation):
    @classmethod
    def read(cls, owner: Thread, path: Path, problem: str,
             suffix: str) -> Iterator[CheckoutAllocation]:
        if path == owner.project_path:
            yield cls()

    def owns_present(self, rule: StorageRule, owner: Thread, path: Path) -> bool:
        return path in owner.home_checkouts


class EditableCheckoutAllocation(CheckoutAllocation):
    """Declared editable members acquire Git identity once at the source-write boundary."""

    def reserves_boundary(self) -> bool:
        return True

    def edit_boundary(self, rule: StorageRule, owner: Thread, path: Path,
                      store: TodoStore) -> frozenset[Path]:
        try:
            repository = Repo.of(path).github
            if not self.owns_declared(rule, owner, path, repository):
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

    def owns_present(self, rule: StorageRule, owner: Thread, path: Path) -> bool:
        return path == rule.order_checkout(rule.roots_of(owner), self.problem)


class RepositoryAllocation(CheckoutAllocation):
    """A repository-suffixed allocation reads its origin once to establish ownership."""

    suffix: str

    @abstractmethod
    def owns_declared(self, rule: StorageRule, owner: Thread, path: Path,
                      repository: RepositoryName) -> bool: ...

    def owns_present(self, rule: StorageRule, owner: Thread, path: Path) -> bool:
        try:
            repository = Repo.of(path).github
        except GitCommandError as error:
            raise StorageDenied(
                f'Cannot read repository {self.suffix} of checkout {path}: {error}') from error
        return self.owns_declared(rule, owner, path, repository)


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
                      repository: RepositoryName) -> bool:
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
                      repository: RepositoryName) -> bool:
        return path == rule.repository_checkout(rule.roots_of(owner), self.goal, repository)


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
    temp_root: StoragePath = Path('/tmp')  # RAM-backed, so its pytest dirs hold memory
    proc: StoragePath = Path('/proc')
    shown: ClassVar[int] = 5  # holders named in each list
    # Home paths are read when the rule is built, never frozen at import.
    volume: StoragePath = field(default_factory=Path.home)
    scratch_base: StoragePath = field(
        default_factory=lambda: Path.home() / '.cache' / 'agent-scratch')
    checkout_base: StoragePath = field(default_factory=lambda: Path.home() / 'wt')

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

    def initial_roots(self, name: str) -> StorageRoots:
        return StorageRoots(self.scratch_base / name)

    def roots_of(self, thread: Thread) -> StorageRoots:
        return thread.storage_roots.assigned(self, thread.name)

    def order_checkout(self, roots: StorageRoots, problem: str) -> Path:
        """Allocation path for a new problem checkout of its order's check repository; an
        existing source may be reused."""
        return self.checkout_base / f'{roots.allocation_name}--{problem}'

    def repository_checkout(self, roots: StorageRoots, problem: str,
                            repository: RepositoryName) -> Path:
        """The same problem's checkout of another repository, changed in lockstep with it."""
        checkout = self.order_checkout(roots, problem)
        return checkout.with_name(f'{checkout.name}--{repository.name}')

    def edit_boundaries(self, owner: Thread, problem: str, primary: RepositoryName,
                        companions: tuple[RepositoryName, ...], generation: GoalId) -> frozenset[Path]:
        """Physical editable roots follow declared allocations, even before materialization."""
        return frozenset(path.resolve()
                         for member in CheckoutAllocation.members_with(CheckoutAllocation)
                         for path in member.for_work(self, owner, problem, primary, companions, generation))

    def problem_checkouts(self, roots: StorageRoots, problem: str) -> tuple[Path, ...]:
        """Every checkout allocated to one problem: its check repository's, then each other's."""
        checkout = self.order_checkout(roots, problem)
        return (checkout, *sorted(checkout.parent.glob(f'{checkout.name}--*')))

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
            storage = UnitStorage.read(self, owner)
            for path in present:
                if not storage.owns(Checkout(path)):
                    self.deny(f'{path} is not a registered Git checkout of {owner.name}')
                work = OrderCheckoutWork(path, owner.incarnation)
                if work.read_checkout().holds_work:
                    found.append(work)
        return tuple(found)

    def owns_checkout(self, owner: Thread, path: Path,
                      repository: RepositoryName) -> bool:
        """Ownership follows the declared repository allocation, before its checkout exists."""
        return any(allocation.owns_declared(self, owner, path, repository)
                   for allocation in CheckoutAllocation.for_path(self, owner, path))

    def owns_present_checkout(self, owner: Thread, path: Path) -> bool:
        """Only a repository-suffixed allocation needs its existing Git origin read."""
        return any(allocation.owns_present(self, owner, path)
                   for allocation in CheckoutAllocation.for_path(self, owner, path))

    def require_transfer_source(self, owner: Thread, source: Path,
                                threads: Iterable[Thread], repository: Repo) -> None:
        """Relocation ownership is not permission to edit the old order."""
        if source.is_symlink() or source != source.resolve() or source.parent != self.checkout_base:
            self.deny(f'{source} is not a physical checkout in {self.checkout_base}')
        if source == repository.checkout.resolve():
            self.deny(f'{source} is the registered repository checkout, not an isolated allocation')
        owners = tuple(thread for thread in threads
                       if (self.owns_present_checkout(thread, source)
                           or Checkout(source) in thread.earlier_projects))
        if tuple(thread.incarnation for thread in owners) != (owner.incarnation,):
            self.deny(f'{source} has foreign or ambiguous checkout owners: '
                      + ', '.join(thread.name for thread in owners))
        if Repo.of(source).github != repository.github:
            self.deny(f'{source} is not a checkout of {repository.github}')
        if not (source / '.git').is_file():
            self.deny(f'{source} is a shared main/home checkout, not a linked worktree')

    def checkout_paths(self, owner: Thread) -> tuple[Path, ...]:
        """Declared home and work allocations; Git metadata supplies their branches."""
        ordered = self.order_checkout(self.roots_of(owner), '*')
        return tuple(path for path in (owner.project_path,
                                       *sorted(ordered.parent.glob(ordered.name)))
                     if path.is_dir() and self.owns_present_checkout(owner, path))

    def checkouts_of(self, thread: Thread) -> tuple[Path, ...]:
        """Read each repository now; directories and names do not register ownership."""
        home = thread.project_path
        candidates = (home, *self.checkout_paths(thread))
        checkouts = {home}
        listed: set[Path] = set()
        for repository in Checkout.git_roots(*candidates):
            if repository in listed:
                continue
            entries = WorkBranches(repository).read_checkout().worktrees()
            listed.update(entries)
            checkouts.update(path for path in entries
                             if path.is_dir() and self.owns_present_checkout(thread, path))
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
            f'same problem: {checkout}--<repository name>), '
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
    def read(cls, rule: StorageRule, thread: Thread) -> UnitStorage:
        home, *extras = rule.checkouts_of(thread)
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


@dataclass
class StorageProjection:
    """One call's lazy snapshot; ordinary writes never need a checkout inventory."""

    rule: StorageRule
    thread: Thread
    comms: Comms

    @cached_property
    def snapshot(self) -> RegistrySnapshot:
        return self.comms.registry.snapshot()

    @cached_property
    def storage(self) -> UnitStorage:
        return UnitStorage.read(self.rule, self.thread)

    def require_write(self, path: Path) -> None:
        self.rule.require_destination(self.thread, path)
        owners = self.snapshot.threads_for(self.thread.incarnation.require_recorded())
        if not owners:
            self.rule.deny('The source writer incarnation is no longer registered.')
        owner, = owners
        resolved = path.resolve()
        sources = Checkout.git_roots(resolved, *resolved.parents)
        store = self.comms.goals.todos
        allowed = frozenset(boundary for root in sources
                            for allocation in CheckoutAllocation.for_path(self.rule, owner, root)
                            for boundary in allocation.edit_boundary(self.rule, owner, root, store))
        if (sources or path.is_relative_to(self.rule.checkout_base)
                or resolved.is_relative_to(owner.project_path.resolve())) and not any(
                resolved.is_relative_to(root) for root in allowed):
            self.rule.deny(f'{path} is a read/launch source, not allocated editable work of {owner.name}.')

    def check_call(self, call: CodingCall) -> tuple[StorageEffect, ...]:
        """Check the call's declared effects against this same lazy owner snapshot."""
        effects = call.storage_effects(self.thread.worktree.path)
        self.check(effects)
        return effects

    def check(self, effects: Sequence[StorageEffect]) -> None:
        """Preflight every effect of one call against this one snapshot."""
        for effect in effects:
            effect.check(self)


@dataclass(frozen=True)
class EditWorkplaces:
    """Allocation edit ownership plus retained edit-review evidence, captured for one operation.

    Review-only check/PR sources preserve original calls; they never grant source writes.
    Zero or multiple review candidates never select a held order. Records stay put.
    """

    rule: StorageRule
    owners: Mapping[ThreadIncarnation, Thread]
    allocated: Mapping[Path, frozenset[str]]
    orders: Mapping[str, Todo] = field(default_factory=dict, kw_only=True)
    review_sources: Mapping[Path, frozenset[str]] = field(default_factory=dict, kw_only=True)

    @staticmethod
    def owner_threads(snapshot: RegistrySnapshot) -> dict[ThreadIncarnation, Thread]:
        return {thread.incarnation: thread for thread in snapshot.threads.values()}

    @classmethod
    def for_assignment(cls, root: Path, snapshot: RegistrySnapshot, held: tuple[Todo, ...],
                       proposed: tuple[Todo, ...]) -> Self:
        """Capture declared physical boundaries without forge reads or a Todo write lock."""
        rule = StorageRule.load(root)
        orders = {todo.id: todo for todo in (*held, *proposed)}
        allocated: dict[Path, set[str]] = {}
        for todo in orders.values():
            companions = todo.order.source_repositories()
            for assignment in todo.current_holder.current_assignments():
                for owner in assignment.holder_in(snapshot):
                    for path in rule.edit_boundaries(owner, todo.id, todo.repo, companions,
                                                     assignment.generation):
                        allocated.setdefault(path, set()).add(todo.id)
        return cls(rule, cls.owner_threads(snapshot),
                   {path: frozenset(problems) for path, problems in allocated.items()},
                   orders=orders)

    def roots_for(self, problem: str) -> frozenset[Path]:
        return frozenset(path for path, problems in self.allocated.items() if problem in problems)

    def require_owner(self, todo: Todo, snapshot: RegistrySnapshot) -> None:
        assignment = todo.held()
        before = frozenset((thread.incarnation, self.rule.roots_of(thread))
                           for thread in self.owners.values()
                           if thread.incarnation == assignment.owner_identity)
        after = frozenset((thread.incarnation, self.rule.roots_of(thread))
                          for thread in assignment.holder_in(snapshot))
        if not before or before != after:
            raise TodoConflict(todo)

    def require_current(self, todo: Todo, snapshot: RegistrySnapshot) -> None:
        if todo.id not in self.orders:
            raise TodoConflict(todo)
        todo.require_result(self.orders[todo.id])
        if self.roots_for(todo.id):
            self.require_owner(todo, snapshot)

    @classmethod
    def capture(cls, comms: Comms, snapshot: RegistrySnapshot, holds: Iterable[LiveHold],
                pulls: ProgressRead
                ) -> dict[ThreadIncarnation, EditWorkplaces]:
        """Acquire source/forge facts once, then match edits without Git or network calls."""
        holds = tuple(holds)
        if not holds:
            return {}
        prepared = cls.for_assignment(comms.root, snapshot, tuple(hold.todo for hold in holds), ())
        repositories = snapshot.commander_project.require().repos
        registered = frozenset(repo.github for repo in repositories)
        rule = prepared.rule
        declared = {todo.id: todo.order.source_repositories() for todo in prepared.orders.values()}
        trees: dict[RepositoryName, dict[str, tuple[str, ...]]] = {}
        for repo in repositories:
            branches = trees.setdefault(repo.github, {})
            for branch, worktrees in pulls.worktrees(repo).items():
                branches[branch] = (*branches.get(branch, ()), *worktrees)
        listed = {Path(path) for branches in trees.values()
                  for worktrees in branches.values() for path in worktrees}
        explicit = {order.check.directory for hold in holds
                    for order in hold.todo.order.declarations}
        links = tuple(dict.fromkeys(link for hold in holds
                                    for order in hold.todo.order.declarations
                                    for link in order.pull_requests))
        requests = {link: row
                    for repo in repositories
                    for row in pulls.rows(repo.github, tuple(
                        link.number for link in links if link.repo == repo.github))
                    for link in links if link.repo == repo.github and link.number == row.number}
        progress = pulls.progress(tuple(hold.todo for hold in holds), comms.registry)
        source_orders: dict[Path, dict[str, Todo]] = {}
        for hold in holds:
            todo = hold.todo
            paths = {order.check.directory for order in todo.order.declarations}
            paths.update(prepared.roots_for(todo.id))
            paths.update(Path(path) for order in todo.order.declarations
                         for link in order.pull_requests if link in requests
                         for path in trees[link.repo].get(requests[link].branch, ()))
            for path in paths:
                source_orders.setdefault(path, {})[todo.id] = todo
        # Qualify only captured Git roots, never nearest parents of individual events.
        # An alias cannot manufacture another problem allocation around the same source.
        present = Checkout.git_roots(*listed, *source_orders)
        actual: dict[Path, Repo] = {}
        for path in present:
            affected = source_orders.get(path, {})
            readable: tuple[Path, ...] = (path,) if affected else ()
            for todo in affected.values():
                readable = progress[todo.id].readable_paths(readable)
            for source in readable:
                if source.is_absolute() and Checkout(source).resolved().path == source:
                    try:
                        repository = Repo.of(source)
                        if repository.github in registered:
                            # A declared source can be a separate clone. Qualify its own
                            # worktree facts within the same per-source failure boundary.
                            extra = (pulls.worktrees(repository)
                                     if source in explicit - listed else {})
                            actual[source] = repository
                            branches = trees[repository.github]
                            for branch, worktrees in extra.items():
                                if str(source) in worktrees:
                                    branches[branch] = (*branches.get(branch, ()), str(source))
                    except (OSError, GitCommandError, WorkBranchError, TimeoutExpired) as error:
                        pulls.unread(affected.values(), error)
        owners = prepared.owners
        # The allocation cut is the only problem ownership map. Qualify its source
        # identity once; absent Git never enables a generic check-root fallback.
        allocated = {
            path: frozenset(
                problem for problem in problems if path in actual
                for assignment in prepared.orders[problem].current_holder.current_assignments()
                for owner in assignment.holder_in(snapshot)
                for allocation in CheckoutAllocation.for_path(rule, owner, path)
                if actual[path].github in allocation.source_repositories(
                    prepared.orders[problem].repo, declared[problem])
                and allocation.owns_declared(rule, owner, path, actual[path].github))
            for path, problems in prepared.allocated.items()
        }
        workplaces: dict[ThreadIncarnation, EditWorkplaces] = {}
        for holder in dict.fromkeys(hold.holder.incarnation for hold in holds):
            sources: dict[Path, set[str]] = {}
            for hold in holds:
                if hold.holder.incarnation != holder:
                    continue
                todo = hold.todo
                linked = tuple(link for order in todo.order.declarations
                               for link in order.pull_requests)
                for order in todo.order.declarations:
                    path = order.check.directory
                    # Explicit pre-PR source is valid only for its primary actual repository,
                    # and ceases being a generic workplace once that source has a PR link.
                    if (path in actual and actual[path].github == todo.repo
                            and not any(link.repo == todo.repo for link in linked)):
                        sources.setdefault(path, set()).add(todo.id)
                for link in linked:
                    if link not in requests:
                        continue  # Unregistered repositories cannot qualify a source boundary.
                    for worktree_path in trees[link.repo].get(requests[link].branch, ()):
                        root = Path(worktree_path)
                        if root in actual and actual[root].github == link.repo:
                            sources.setdefault(root, set()).add(todo.id)
            workplaces[holder] = cls(rule, owners, allocated, review_sources={
                path: frozenset(problems) for path, problems in sources.items()})
        return workplaces

    def problems_for(self, path: str) -> frozenset[str]:
        original = Path(path)
        if not original.is_absolute():
            return frozenset()
        # Resolve the edited path once, not its missing leaf with Git. Lexical membership
        # rejects aliases into a checkout; resolved membership rejects symlink escapes.
        resolved = original.resolve()
        parts = (original.relative_to(self.rule.checkout_base).parts
                 if original.is_relative_to(self.rule.checkout_base) else ())
        boundary = self.rule.checkout_base.joinpath(*parts[:1])
        reserved = boundary in self.allocated or any(allocation.reserves_boundary()
                       for thread in self.owners.values()
                       for prefix in (self.rule.order_checkout(self.rule.roots_of(thread), ''),)
                       if boundary.parent == prefix.parent and boundary.name.startswith(prefix.name)
                       for allocation in CheckoutAllocation.for_path(self.rule, thread, boundary))
        # Even a missing/released/unknown allocation reserves its lexical boundary. Only
        # captured actual Git identity may supply an owner; absence never enables spill.
        candidates = ({boundary: self.allocated.get(boundary, frozenset())}
                      if reserved else self.review_sources)
        return frozenset(problem for root, problems in candidates.items()
                         if original.is_relative_to(root) and resolved.is_relative_to(root)
                         for problem in problems)

