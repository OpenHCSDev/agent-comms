"""Configured storage limits over current filesystem and Git worktrees."""
from __future__ import annotations

import json
import math
import shutil
import time
from abc import abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.declared_family import DeclaredFamily
from agent_comms.child_process import ResidentMemory
from agent_comms.field_codec import (
    FieldCodec,
    GoalId,
    MalformedRecord,
)
from agent_comms.locked_store import DurableFile
from agent_comms.problem_number import ProblemNumber
from agent_comms.repo import Checkout, GitCommandError, Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.storage_room import RoomAllocation
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.work_branches import WorkBranches

from .shell_effects import StorageDenied, StorageEffect
from .thread_storage import (
    StoragePath,
    StorageRoots,
)
from .threads import Thread

if TYPE_CHECKING:
    from agent_comms.registration import Registration
    from agent_comms.todos import Todo, TodoStore

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
        problem, _, suffix = path.name.removeprefix(f'{allocated}--').partition('--')
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


@dataclass(frozen=True)
class HomeCheckoutAllocation(CheckoutAllocation):
    @classmethod
    def read(cls, owner: Thread, path: Path, problem: str,
             suffix: str) -> Iterator[CheckoutAllocation]:
        if path == owner.project_path:
            yield cls()

    def owns_present(self, rule: StorageRule, owner: Thread, path: Path) -> bool:
        return path in owner.home_checkouts


class ProblemCheckoutAllocation(CheckoutAllocation):
    """A checkout allocated to one problem; ``verb`` is the command that creates it."""

    verb: ClassVar[str]

    @classmethod
    def read(cls, owner: Thread, path: Path, problem: str,
             suffix: str) -> Iterator[CheckoutAllocation]:
        if path != owner.project_path and ProblemNumber.accepts(problem):
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
    def allocated(cls, problem: str, suffix: str) -> Iterator[CheckoutAllocation]:
        if suffix:
            yield cls(problem, suffix)

    def owns_declared(self, rule: StorageRule, owner: Thread, path: Path,
                      repository: RepositoryName) -> bool:
        return path == rule.repository_checkout(rule.roots_of(owner), self.problem, repository)


@dataclass(frozen=True)
class GoalCheckoutAllocation(RepositoryAllocation):
    goal: GoalId
    suffix: str

    @classmethod
    def read(cls, owner: Thread, path: Path, problem: str,
             suffix: str) -> Iterator[CheckoutAllocation]:
        if path == owner.project_path or not suffix:
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

    def checkout_problem(self, roots: StorageRoots, path: Path, other: str) -> str:
        """The problem whose checkout holds path, read back from order_checkout's own name;
        other for a path in no problem checkout."""
        allocated = self.order_checkout(roots, '')
        names = path.relative_to(allocated.parent).parts[:1] if path.is_relative_to(allocated.parent) else ()
        return next((name.removeprefix(allocated.name).split('--')[0] for name in names
                     if name.startswith(allocated.name)), other)

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

    def require_path(self, thread: Thread, path: Path) -> None:
        """A write in a present checkout follows that checkout's allocation."""
        self.require_destination(thread, path)
        base = self.checkout_base
        if path.is_relative_to(base) and path != base:
            checkout = base / path.relative_to(base).parts[0]
            if not self.owns_present_checkout(thread, checkout):
                self.deny(f'{checkout} is not a checkout of {thread.name}')

    def order_text(self, thread: Thread, pythons: Sequence[str]) -> str:
        """The rule uses the same declared paths as storage enforcement."""
        checkout = self.order_checkout(self.roots_of(thread), "P<n>")
        return (
            f'Storage (refused without a strike): your project is {thread.project_path}; '
            f'your checkouts are '
            f'{checkout} (another repository changed in the '
            f'same problem: {checkout}--<repository name>), '
            f'created with agent-comms {OrderCheckoutAllocation.verb} add PROBLEM '
            f'(another repository: agent-comms {RepositoryCheckoutAllocation.verb} add '
            'PROBLEM OWNER/REPO), '
            f'removed with agent-comms {OrderCheckoutAllocation.verb} remove PROBLEM in the '
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

    @cached_property
    def storage(self) -> UnitStorage:
        return UnitStorage.read(self.rule, self.thread)

    def check_call(self, call: CodingCall) -> tuple[StorageEffect, ...]:
        """Check the call's declared effects against this same lazy owner snapshot."""
        effects = call.storage_effects(self.thread.worktree.path)
        self.check(effects)
        return effects

    def check(self, effects: Sequence[StorageEffect]) -> None:
        """Preflight every effect of one call against this one snapshot."""
        for effect in effects:
            effect.check(self)
