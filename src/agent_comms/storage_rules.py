"""Configured storage limits over current filesystem and Git worktrees."""
from __future__ import annotations

import getopt
import json
import math
import re
import shutil
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Self, cast

from pydantic import JsonValue

from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import RelationViolationError, ToolRefusedError
from agent_comms.field_codec import (
    FieldCodec,
    FieldRepresentation,
    GoalId,
    MalformedRecord,
    NullMember,
    PathText,
    RecordMember,
    SubclassUnion,
)
from agent_comms.locked_store import DurableFile
from agent_comms.problem_number import ProblemNumber
from agent_comms.repo import GitCommandError, Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.standing_rules import FamilyScan, UndatedStandingRule
from agent_comms.storage_room import (
    AllocationRoomWork,
    BuildRoomWork,
    RoomAllocation,
    TestGateRoomWork,
)
from agent_comms.tool_denial import SelectedToolDenied
from agent_comms.work_branches import WorkBranches
from agent_comms_hooks.shell import ShellCommand

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.heartbeat_frame import HeartbeatFrame
    from agent_comms.registration import Registration
    from agent_comms.thread_identity import ThreadIncarnation
    from agent_comms.threads import Thread
    from agent_comms.todos import Todo, TodoStore
    from agent_comms.ui_model.unit_state import UnitState

StoragePath = Annotated[Path, PathText]


class ThreadStorage(SubclassUnion, affix="ThreadStorage"):
    """A thread's storage roots: the ones its registry assigned, or none assigned yet."""

    @abstractmethod
    def assigned(self, rule: StorageRule, name: str) -> StorageRoots:
        """The roots thread ``name`` holds under ``rule``."""

    @abstractmethod
    def stored(self) -> StorageRoots:
        """The roots a registry row records."""


@dataclass(frozen=True, slots=True)
class StorageRoots(RecordMember, ThreadStorage):
    scratch: StoragePath

    def __post_init__(self) -> None:
        if not self.scratch.is_absolute():
            raise ValueError("Storage scratch requires an absolute path.")

    def assigned(self, rule: StorageRule, name: str) -> StorageRoots:
        return self

    def stored(self) -> StorageRoots:
        return self

    @property
    def allocation_name(self) -> str:
        """The name the registry gave this thread's storage at registration. Its checkouts
        carry it, so a renamed thread still owns the work it allocated."""
        return self.scratch.name


@dataclass(frozen=True, slots=True)
class UnassignedThreadStorage(NullMember, ThreadStorage):
    """A thread not yet declared; its registry assigns its roots at declaration."""

    def assigned(self, rule: StorageRule, name: str) -> StorageRoots:
        return rule.initial_roots(name)

    def stored(self) -> StorageRoots:
        raise RelationViolationError(
            "A thread is stored only after its registry assigned its storage roots."
        )


class AssignedStorageRoots(FieldRepresentation):
    """A stored thread holds the roots its registry assigned at registration, so its
    stored form is StorageRoots; only an unregistered thread in memory has none."""

    @classmethod
    def decode(cls, value: object) -> StorageRoots:
        return FieldCodec.decode(StorageRoots, value)

    @classmethod
    def encode(cls, value: object) -> object:
        return FieldCodec.encode(cast(ThreadStorage, value).stored())

    @classmethod
    def schema(cls) -> dict[str, JsonValue]:
        return FieldCodec.record_schema(StorageRoots)


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
        return True


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
    floor_gib: float = 5.0  # under this nothing that allocates may run
    cleanup_gib: float = 30.0  # under this a cleanup owner is due; it does not stop builds
    memory_gib: float = 3.0  # available memory under this makes a cleanup owner due
    idle_minutes: float = 30.0  # a pytest temp dir untouched this long holds memory for nothing
    temp_root: StoragePath = Path('/tmp')  # RAM-backed, so its pytest dirs hold memory
    proc: StoragePath = Path('/proc')
    shown: ClassVar[int] = 5  # holders named in each list
    scratch_cap_gib: float = 3.0  # one unit's scratch together with its checkouts
    max_checkouts: int = 2
    # Home paths are read when the rule is built, never frozen at import.
    volume: StoragePath = field(default_factory=Path.home)
    scratch_base: StoragePath = field(
        default_factory=lambda: Path.home() / '.cache' / 'agent-scratch')
    checkout_base: StoragePath = field(default_factory=lambda: Path.home() / 'wt')

    def __post_init__(self):
        limits = (self.floor_gib, self.cleanup_gib, self.scratch_cap_gib)
        if not all(math.isfinite(n) for n in limits):
            raise ValueError("Storage limits must be finite.")
        if min(*limits, self.max_checkouts) < 0:
            raise ValueError("Storage limits cannot be negative.")

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
        return self.checkout_base.resolve() / f'{roots.allocation_name}--{problem}'

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
                if path not in (storage.home, *storage.extra_checkouts):
                    self.deny(f'{path} is not a registered Git checkout of {owner.name}')
                work = OrderCheckoutWork(path, owner)
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
        for repository in candidates:
            if repository in listed or not (repository / '.git').exists():
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
                status = (pid / 'status').read_text().splitlines()
            except (FileNotFoundError, ProcessLookupError):
                continue
            rss = [line.split()[1] for line in status if line.startswith('VmRSS:')]
            if rss and any(b'pytest' in word for word in argv):
                yield MemoryHolder(f'pid {pid.name}', int(rss[0]) / 2**20)

    def memory_due(self, memory: MemoryUse) -> bool:
        return memory.available_gib < self.memory_gib

    def below_floor(self, free_gib: float) -> bool:
        return free_gib < self.floor_gib

    def cleanup_due(self, free_gib: float) -> bool:
        return free_gib < self.cleanup_gib

    def require_quota(self, thread: Thread, *proposed: Path):
        UnitStorage.read(self, thread, *proposed).require_quota()

    def require_floor(self) -> None:
        if self.below_floor(self.free_gib()):
            self.deny(f'allocation requires {self.floor_gib:g} GiB free on {self.volume}')

    def require_allocation(self, thread: Thread, *proposed: Path):
        self.require_floor()
        self.require_quota(thread, *proposed)

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
        base = self.checkout_base.resolve()
        if path.is_relative_to(base) and path != base:
            checkout = base / path.relative_to(base).parts[0]
            if not self.owns_present_checkout(thread, checkout):
                self.deny(f'{checkout} is not a checkout of {thread.name}')

    def order_text(self, thread: Thread, pythons: Sequence[str]) -> str:
        """The rule uses the same declared paths as storage enforcement."""
        checkout = self.order_checkout(self.roots_of(thread), "P<n>")
        return (
            f'Storage (refused without a strike): your checkouts are '
            f'{checkout} (another repository changed in the '
            f'same problem: {checkout}--<repository name>), '
            f'at most {self.max_checkouts} besides your home checkout {thread.project_path}, '
            f'created with agent-comms {OrderCheckoutAllocation.verb} add PROBLEM '
            f'(another repository: agent-comms {RepositoryCheckoutAllocation.verb} add '
            'PROBLEM OWNER/REPO), '
            f'removed with agent-comms {OrderCheckoutAllocation.verb} remove PROBLEM in the '
            'same order that created them; '
            'your only scratch dir is '
            f'{self.roots_of(thread).scratch}/, '
            f'capped at {self.scratch_cap_gib:g} GB together with '
            f'your checkouts. Run tests with PYTHONPATH=src on the shared {" or ".join(pythons)}; '
            'never create a venv, never uv sync, never write .artifacts, never git clone. '
            f'Below {self.floor_gib:g} GB free on {self.volume}, no worktree, venv, build, install '
            'or recording.'
        )


@dataclass(frozen=True)
class OrderCheckoutWork(WorkBranches):
    """An owned order checkout uses the existing Git-work reader, not another Git policy."""

    owner: Thread


@dataclass(frozen=True)
class UnitStorage:
    """The declared home (possibly non-Git) and separately counted extra Git checkouts."""

    rule: StorageRule
    unit: Thread
    home: Path
    extra_checkouts: tuple[Path, ...]

    @classmethod
    def read(cls, rule: StorageRule, thread: Thread, *proposed: Path) -> UnitStorage:
        home, *extras = rule.checkouts_of(thread)
        return cls(rule, thread, home,
                   tuple(dict.fromkeys(path for path in (*extras, *proposed) if path != home)))

    @property
    def over_checkouts(self) -> bool:
        return len(self.extra_checkouts) > self.rule.max_checkouts

    @property
    def missing_home(self) -> bool:
        return not self.home.is_dir()

    def adding(self, path: Path) -> UnitStorage:
        proposed = replace(self, extra_checkouts=tuple(dict.fromkeys(
            checkout for checkout in (*self.extra_checkouts, path) if checkout != self.home)))
        proposed.require_quota()
        return proposed

    def removing(self, path: Path) -> UnitStorage:
        if path not in self.extra_checkouts:
            self.rule.deny('only a current extra checkout can be removed')
        return replace(self, extra_checkouts=tuple(p for p in self.extra_checkouts if p != path))

    def require_quota(self) -> None:
        if self.over_checkouts:
            self.rule.deny(f'at most {self.rule.max_checkouts} checkouts besides the home checkout')

    def require_retired(self, problem: str) -> None:
        for path in self.rule.problem_checkouts(self.rule.roots_of(self.unit), problem):
            if path in self.extra_checkouts:
                self.rule.deny(
                    f'{self.unit.name} still has the checkout {path} for this order; '
                    'it removes it before the order counts as done')
        self.require_quota()


@dataclass(frozen=True)
class StorageViolation(UndatedStandingRule, FamilyScan, declared_name=None):
    """One family scan shares each held unit's storage snapshot between its violations."""

    storage: UnitStorage

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        # Storage reads the filesystem, not the git source an unreadable card failed on:
        # a unit whose declared home is gone is that card and a missing home.
        held = frame.holders
        return cls.for_units(comms, (state.unit for state in observed if state.unit in held))

    @classmethod
    def for_units(cls, comms: Comms, units: Iterable[ThreadIncarnation]) -> tuple[Self, ...]:
        registry = comms.registry
        members = cls.members_with(cls)
        found = []
        for unit in sorted(set(units), key=lambda unit: unit.name):
            thread = registry.require(unit.name)
            if thread.incarnation != unit:
                raise ValueError("Observed unit incarnation changed")
            storage = UnitStorage.read(registry.storage_rule, thread)
            found.extend(member(storage) for member in members if member.violated(storage))
        return tuple(found)

    @classmethod
    @abstractmethod
    def violated(cls, storage: UnitStorage) -> bool: ...


class StorageOverQuota(StorageViolation):
    @classmethod
    def violated(cls, storage: UnitStorage) -> bool:
        return storage.over_checkouts

    def line(self) -> str:
        return (f'VIOLATION storage quota: {self.storage.unit.name} holds '
                f'{len(self.storage.extra_checkouts)} checkouts besides home, '
                f'limit {self.storage.rule.max_checkouts} -> order the cleanup')


class StorageHomeMissing(StorageViolation):
    @classmethod
    def violated(cls, storage: UnitStorage) -> bool:
        return storage.missing_home

    def line(self) -> str:
        return (f'VIOLATION storage home: {self.storage.unit.name} declared home '
                f'{self.storage.home} is missing -> restore the home checkout')


@dataclass
class StorageProjection:
    """One call's lazy snapshot; ordinary writes never need a checkout inventory."""

    rule: StorageRule
    thread: Thread

    @cached_property
    def storage(self) -> UnitStorage:
        return UnitStorage.read(self.rule, self.thread)

    def check(self, effects: Sequence[StorageEffect]) -> None:
        """Preflight every effect of one call against this one snapshot."""
        for effect in effects:
            effect.check(self)


class ShellProgram:
    names: ClassVar[frozenset[str]] = frozenset()
    programs: ClassVar[dict[str, type[ShellProgram]]] = {}
    versioned = False

    def __init_subclass__(cls):
        super().__init_subclass__()
        for name in cls.__dict__.get('names', ()):
            ShellProgram.programs[name] = cls

    @classmethod
    def named(cls, name):
        kind = cls.programs.get(name if name in cls.programs else re.sub(r'\d+(?:\.\d+)*$', '', name))
        return (kind if kind and (kind.versioned or name in kind.names) else cls)()

    def matches(self, args, prefixes):
        return any(tuple(args[:len(prefix)]) == prefix for prefix in prefixes)

    def effects(self, args, cwd):
        return tuple(effect for owner in type(self).__mro__
                     if (decode := owner.__dict__.get('decode')) is not None
                     for effect in decode(self, args, cwd))


class StorageDenied(SelectedToolDenied, ToolRefusedError):  # noqa: N818 - existing nominal refusal
    """The storage preflight refused an operation before it executed."""


class StorageEffect(ABC):
    """Preflight one actual operation and declare its measured work, if any."""
    work: ClassVar[tuple] = ()

    @abstractmethod
    def check(self, projection: StorageProjection) -> None:
        """Refuse this operation if the thread's storage cannot take it."""


@dataclass(frozen=True)
class WriteStorageEffect(StorageEffect):
    path: Path

    def check(self, projection: StorageProjection):
        projection.rule.require_path(projection.thread, self.path)


@dataclass(frozen=True)
class ForbiddenStorageEffect(StorageEffect):
    reason: str

    def check(self, projection: StorageProjection):
        projection.rule.deny(self.reason)


@dataclass(frozen=True)
class AllocateStorageEffect(StorageEffect):
    work = (AllocationRoomWork,)

    def check(self, projection: StorageProjection):
        projection.rule.require_floor()
        projection.storage.require_quota()


class BuildStorageEffect(AllocateStorageEffect):
    work = (BuildRoomWork,)


class TestGateStorageEffect(AllocateStorageEffect):
    work = (TestGateRoomWork,)


@dataclass(frozen=True)
class CheckoutStorageEffect(AllocateStorageEffect):
    """A checkout allocated to a declared repository, before Git creates its directory."""

    path: Path
    repository: RepositoryName

    def check(self, projection: StorageProjection):
        rule, thread = projection.rule, projection.thread
        rule.require_destination(thread, self.path)
        if not rule.owns_checkout(thread, self.path, self.repository):
            destination = rule.order_checkout(rule.roots_of(thread), "P<number>")
            rule.deny(f'checkout destination must be {destination}')
        super().check(projection)
        projection.storage = projection.storage.adding(self.path)


@dataclass(frozen=True)
class RemoveCheckoutStorageEffect(StorageEffect):
    path: Path

    def check(self, projection: StorageProjection):
        if self.path == projection.thread.project_path:
            projection.rule.deny(f'{self.path} is where {projection.thread.name} runs; choose another existing project first: agent-comms set-project {projection.thread.name} <existing-project>')
        projection.storage = projection.storage.removing(self.path)


def physical_path(value, cwd):
    if '$' in value or '`' in value:
        raise StorageDenied(f'StorageRule: use a literal storage destination, not {value!r}')
    path = Path(value).expanduser()
    return (path if path.is_absolute() else cwd / path).resolve()


@dataclass(frozen=True)
class ShellInvocation(ShellCommand):
    """One decoded shell command and the directory its preceding commands select."""

    cwd: Path

    @classmethod
    def parse(cls, command: str, cwd: Path) -> Iterator[ShellInvocation]:
        try:
            decoded_commands = tuple(ShellCommand.split(command))
        except ValueError as error:
            raise StorageDenied(f'StorageRule: the shell command cannot be decoded ({error}); '
                                'no tool was started') from error
        for decoded in decoded_commands:
            words = decoded.words
            if decoded.program == 'cd' and len(words) > 1:
                target = words[2] if words[1] == '--' and len(words) > 2 else words[1]
                cwd = physical_path(target, cwd)
                continue
            yield cls(words, cwd)


@dataclass(frozen=True)
class GitInvocation:
    args: list[str]
    cwd: Path


class ForbidsCommands(ShellProgram):
    forbidden = ((),)
    reason = 'virtualenv creation and uv sync are forbidden; use the shared environment'

    def decode(self, args, cwd):
        return (ForbiddenStorageEffect(self.reason),) if self.matches(args, self.forbidden) else ()


class AllocatesOnCommands(ShellProgram):
    prefixes = ((),)

    def decode(self, args, cwd):
        if not {'--help', '-h'}.isdisjoint(args):
            return ()  # asking a tool for its help allocates nothing
        return (AllocateStorageEffect(),) if self.matches(args, self.prefixes) else ()


class MeasuredCommands(ShellProgram):
    """Compose command declarations, including sibling capabilities in the MRO."""

    def decode(self, args, cwd):
        if not {'--help', '-h'}.isdisjoint(args):
            return ()
        return tuple(owner.effect() for owner in type(self).__mro__
                     if 'commands' in owner.__dict__ and any(
                         self.matches((name, *args), owner.commands) for name in self.names))


class BuildsOnCommands(MeasuredCommands):
    effect = BuildStorageEffect
    commands = (('build_runtime.py',), ('python', 'build_runtime.py'),
                ('python', '-m', 'hatchling', 'build'), ('python', '-m', 'build'))


class TestsOnCommands(MeasuredCommands):
    effect = TestGateStorageEffect
    commands = (('pytest',), ('py.test',), ('python', '-m', 'pytest'),
                ('python', 'test_gate.py'))


class WritesTargets(ShellProgram):
    targets = slice(None)
    minimum = 1

    def decode(self, args, cwd):
        paths = [arg for arg in args if not arg.startswith('-')]
        return tuple(WriteStorageEffect(physical_path(p, cwd)) for p in paths[self.targets]) if len(paths) >= self.minimum else ()


class WritesWorkingTree(ShellProgram):
    verbs = (('switch',), ('checkout',), ('commit',),
             ('merge',), ('reset',), ('stash',))

    def decode(self, args: list[str], cwd: Path) -> tuple[StorageEffect, ...]:
        return (WriteStorageEffect(cwd),) if self.matches(args, self.verbs) else ()


class GitProgram(ForbidsCommands, WritesWorkingTree):
    names = frozenset({'git'})
    forbidden = (('clone',), ('worktree', 'add'), ('worktree', 'move'), ('worktree', 'remove'))
    reason = ('git clone and git worktree add, move and remove are forbidden; run agent-comms '
              'checkout add PROBLEM or checkout remove PROBLEM, which own the pinned submodules')

    chdir_option = '-C'
    directory_options = frozenset({chdir_option, '--git-dir', '--work-tree'})
    value_options = (*sorted(directory_options), '-c', '--config-env', '--namespace',
                     '--list-cmds', '--attr-source', '--super-prefix')
    optional_options = ('--exec-path',)
    switch_options = ('-v', '-h', '-p', '-P', '--version', '--help', '--no-pager', '--paginate',
                      '--html-path', '--man-path', '--info-path', '--no-replace-objects', '--bare',
                      '--literal-pathspecs', '--glob-pathspecs', '--noglob-pathspecs',
                      '--icase-pathspecs', '--no-optional-locks', '--no-lazy-fetch', '--no-advice')

    @classmethod
    def invocation(cls, args: list[str], cwd: Path) -> GitInvocation:
        short = ''.join(name[1:] + ':' for name in cls.value_options if len(name) == 2)
        short += ''.join(name[1:] for name in cls.switch_options if len(name) == 2)
        long = [name[2:] + '=' for name in cls.value_options if name.startswith('--')]
        long.extend(name[2:] for name in cls.switch_options if name.startswith('--'))
        long.extend(name[2:] + '=?' for name in cls.optional_options)
        try:
            options, remaining = getopt.getopt(args, short, long)
        except getopt.GetoptError as error:
            raise SelectedToolDenied(
                f'StorageRule: incomplete or unknown git option: {error}') from error
        for option, value in options:
            if option in cls.directory_options:
                path = physical_path(value, cwd)
                if option == cls.chdir_option:
                    cwd = path
        return GitInvocation(remaining, cwd)

    def effects(self, args, cwd):
        invocation = self.invocation(args, cwd)
        return super().effects(invocation.args, invocation.cwd)


class PythonProgram(ForbidsCommands, BuildsOnCommands, TestsOnCommands, AllocatesOnCommands):
    names = frozenset({'python'})
    versioned = True
    forbidden = (('-m', 'venv'),)
    prefixes = (('-m', 'pip', 'install'),)

    def effects(self, args, cwd):
        while args and args[0] in ('-I', '-B', '-u', '-E', '-s'):
            args = args[1:]
        args = [Path(args[0]).name, *args[1:]] if args and not args[0].startswith('-') else args
        return super().effects(args, cwd)


class UvProgram(ForbidsCommands, AllocatesOnCommands):
    names = frozenset({'uv'})
    forbidden = (('venv',), ('sync',))
    prefixes = (('pip', 'install'),)


class MakeEnvironmentProgram(ForbidsCommands):
    names = frozenset({'virtualenv'})


class DiskProgram(AllocatesOnCommands):
    names = frozenset({'ffmpeg', 'recordmydesktop'})


class BuildProgram(BuildsOnCommands):
    names = frozenset({'build_runtime.py'})


class TestGateProgram(TestsOnCommands):
    names = frozenset({'pytest', 'py.test'})


class PackageProgram(AllocatesOnCommands):
    names = frozenset({'pip', 'pip3', 'npm', 'pnpm', 'yarn'})
    prefixes = (('install',), ('ci',), ('add',))


class ProfileProgram(AllocatesOnCommands):
    names = frozenset({'py-spy'})
    prefixes = (('record',),)


class FileWriterProgram(WritesTargets):
    names = frozenset({'mkdir', 'touch', 'tee'})


class CopyProgram(WritesTargets):
    names = frozenset({'cp', 'mv', 'rsync', 'install', 'ln'})
    targets = slice(-1, None)
    minimum = 2


def shell_storage_effects(command, cwd):
    """Decode known shell operations, not arbitrary interpreter programs."""
    effects = []
    for invocation in ShellInvocation.parse(command, cwd):
        args = invocation.args
        for index, word in enumerate(args[:-1]):
            if word in ('>', '>>'):
                effects.append(WriteStorageEffect(physical_path(args[index + 1], invocation.cwd)))
        effects.extend(ShellProgram.named(invocation.program).effects(args, invocation.cwd))
    return tuple(effects)
