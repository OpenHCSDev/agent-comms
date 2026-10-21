"""Configured storage limits over current filesystem and Git worktrees."""
from __future__ import annotations

import getopt
import json
import math
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import cached_property
from itertools import groupby
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from .field_codec import FieldCodec, PathText
from .native_tool_call import SelectedToolDenied
from .standing_rules import UndatedStandingRule
from .storage_room import (
    AllocationRoomWork, BuildRoomWork, RoomAllocation, TestGateRoomWork,
)

if TYPE_CHECKING:
    from .comms import Comms
    from .thread_identity import ThreadIncarnation
    from .threads import Thread

StoragePath = Annotated[Path, PathText]


@dataclass(frozen=True)
class StorageRoots:
    scratch: StoragePath

    def __post_init__(self):
        if not self.scratch.is_absolute():
            raise ValueError("Storage scratch requires an absolute path.")


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


@dataclass(frozen=True)
class StorageRule(RoomAllocation):
    floor_gib: float = 5.0  # under this nothing that allocates may run
    cleanup_gib: float = 30.0  # under this a cleanup owner is due; it does not stop builds
    memory_gib: float = 3.0  # available memory under this makes a cleanup owner due
    idle_minutes: float = 30.0  # a pytest temp dir untouched this long holds memory for nothing
    temp_root: StoragePath = Path('/tmp')  # RAM-backed, so its pytest dirs hold memory
    proc: StoragePath = Path('/proc')
    shown: ClassVar[int] = 5  # holders named in each list
    scratch_cap_gib: float = 3.0  # one unit's scratch together with its checkouts
    max_checkouts: int = 2
    volume: StoragePath = Path.home()
    scratch_base: StoragePath = Path.home() / '.cache' / 'agent-scratch'
    checkout_base: StoragePath = Path.home() / 'wt'

    def __post_init__(self):
        limits = (self.floor_gib, self.cleanup_gib, self.scratch_cap_gib)
        if not all(math.isfinite(n) for n in limits):
            raise ValueError("Storage limits must be finite.")
        if min(*limits, self.max_checkouts) < 0:
            raise ValueError("Storage limits cannot be negative.")

    @classmethod
    def load(cls, root: Path):
        path = root / 'storage.json'
        return FieldCodec.decode(cls, json.loads(path.read_text()) if path.exists() else {})

    def initial_roots(self, name):
        return StorageRoots(self.scratch_base / name)

    def order_checkout(self, name: str, problem: str) -> Path:
        """Allocation path for a new problem checkout; an existing source may be reused."""
        return self.checkout_base.resolve() / f'{name}--{problem}'

    def owns_checkout(self, owner: ThreadIncarnation, path: Path) -> bool:
        problem = path.name.removeprefix(f'{owner.name}--')
        return (path == self.checkout_base.resolve() / owner.name
                or (bool(re.fullmatch(r'P[1-9][0-9]*', problem))
                    and path == self.order_checkout(owner.name, problem)))

    def checkout_paths(self, owner: ThreadIncarnation) -> tuple[Path, ...]:
        """Candidate locations; Git worktree metadata supplies their branches."""
        ordered = self.order_checkout(owner.name, 'P*')
        return tuple(path for path in (self.checkout_base.resolve() / owner.name,
                                       *sorted(ordered.parent.glob(ordered.name)))
                     if path.is_dir() and self.owns_checkout(owner, path))

    def checkouts_of(self, thread: Thread) -> tuple[Path, ...]:
        """Read each repository now; directories and names do not register ownership."""
        home = self.checkout_base.expanduser().resolve() / thread.name
        candidates = (home, Path(thread.worktree).expanduser().resolve(),
                      *self.checkout_paths(thread.incarnation))
        checkouts = {home}
        listed = set()
        for repository in candidates:
            if repository in listed or not (repository / '.git').exists():
                continue
            output = subprocess.run(
                ('git', '-C', str(repository), 'worktree', 'list', '--porcelain', '-z'),
                capture_output=True, text=True, check=True, timeout=10,
            ).stdout
            entries = tuple(Path(field.removeprefix('worktree ')).resolve()
                            for field in output.split('\0') if field.startswith('worktree '))
            listed.update(entries)
            checkouts.update(path for path in entries
                             if path.is_dir() and self.owns_checkout(thread.incarnation, path))
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

    def require_path(self, thread: Thread, path):
        if '.artifacts' in path.parts:
            self.deny('writes inside .artifacts are forbidden')
        if path.is_relative_to(self.scratch_base) and not path.is_relative_to(thread.storage_roots.scratch):
            self.deny(f'foreign scratch destination {path}; registered scratch is {thread.storage_roots.scratch}')
        base = self.checkout_base.resolve()
        if path.is_relative_to(base) and path != base:
            checkout = base / path.relative_to(base).parts[0]
            if not self.owns_checkout(thread.incarnation, checkout):
                self.deny(f'{checkout} is not a checkout of {thread.name}')

    def render(self, roots):
        return (f'StorageRule: scratch {roots.scratch}; '
                f'{self.max_checkouts} extra checkouts; allocation floor {self.floor_gib:g} GiB. '
                'No clones, virtualenv creation, uv sync or .artifacts writes.')

    def order_text(self, unit: str, pythons: Sequence[str]) -> str:
        """The rule as an order states it to ``unit``."""
        home = self.checkout_base / unit
        return (
            f'Storage (refused without a strike): your checkouts are {home}--P<n>, at most '
            f'{self.max_checkouts} besides your home checkout {home}, removed with git worktree '
            'remove in the same order that created them; your only scratch dir is '
            f'{self.scratch_base / unit}/, capped at {self.scratch_cap_gib:g} GB together with '
            f'your checkouts. Run tests with PYTHONPATH=src on the shared {" or ".join(pythons)}; '
            'never create a venv, never uv sync, never write .artifacts, never git clone. '
            f'Below {self.floor_gib:g} GB free on {self.volume}, no worktree, venv, build, install '
            'or recording.'
        )


@dataclass(frozen=True)
class UnitStorage(UndatedStandingRule):
    """The home storage slot (possibly non-Git) followed by extra Git checkouts."""

    rule: StorageRule
    unit: str
    checkouts: tuple[Path, ...]

    @classmethod
    def read(cls, rule: StorageRule, thread: Thread, *proposed: Path) -> UnitStorage:
        return cls(rule, thread.name, tuple(dict.fromkeys((*rule.checkouts_of(thread), *proposed))))

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[UnitStorage, ...]:
        todos = cls.todos(comms, commander)
        units = sorted({todo.holder for todo in todos if todo.state.open and todo.holder})
        registry = comms.registry
        read = (cls.read(registry.storage_rule, registry.require(unit)) for unit in units)
        return tuple(storage for storage in read if storage.over_checkouts or storage.missing_home)

    @property
    def extra_checkouts(self) -> tuple[Path, ...]:
        return self.checkouts[1:]

    @property
    def over_checkouts(self) -> bool:
        return len(self.extra_checkouts) > self.rule.max_checkouts

    @property
    def missing_home(self) -> bool:
        return not self.checkouts[0].is_dir()

    def adding(self, path: Path) -> UnitStorage:
        proposed = replace(self, checkouts=tuple(dict.fromkeys((*self.checkouts, path))))
        proposed.require_quota()
        return proposed

    def removing(self, path: Path) -> UnitStorage:
        if path not in self.extra_checkouts:
            self.rule.deny('only a current extra checkout can be removed')
        return replace(self, checkouts=tuple(p for p in self.checkouts if p != path))

    def require_quota(self) -> None:
        if self.over_checkouts:
            self.rule.deny(f'at most {self.rule.max_checkouts} checkouts besides the home checkout')

    def line(self) -> str:
        home = f'; declared home {self.checkouts[0]} is missing' if self.missing_home else ''
        return (f'VIOLATION storage quota: {self.unit} holds {len(self.extra_checkouts)} checkouts besides home, '
                f'limit {self.rule.max_checkouts}{home} -> order the cleanup')


@dataclass
class StorageProjection:
    """One call's lazy snapshot; ordinary writes never need a checkout inventory."""

    rule: StorageRule
    thread: Thread

    @cached_property
    def storage(self) -> UnitStorage:
        return UnitStorage.read(self.rule, self.thread)


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


class StorageDenied(SelectedToolDenied):
    """The storage preflight refused an operation before it executed."""


class StorageEffect:
    """Preflight one actual operation and declare its measured work, if any."""
    work: ClassVar[tuple] = ()


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
    path: Path

    def check(self, projection: StorageProjection):
        rule, thread = projection.rule, projection.thread
        rule.require_path(thread, self.path)
        if not rule.owns_checkout(thread.incarnation, self.path):
            destination = rule.order_checkout(thread.name, "P<number>")
            rule.deny(f'checkout destination must be {destination}')
        super().check(projection)
        projection.storage = projection.storage.adding(self.path)


@dataclass(frozen=True)
class RemoveCheckoutStorageEffect(StorageEffect):
    path: Path

    def check(self, projection: StorageProjection):
        if self.path == projection.thread.project_path:
            projection.rule.deny(f'{self.path} is where {projection.thread.name} runs; move it home first: agent-comms set-project {projection.thread.name} {projection.rule.checkout_base / projection.thread.name}')
        projection.storage = projection.storage.removing(self.path)


def physical_path(value, cwd):
    if '$' in value or '`' in value:
        raise StorageDenied('StorageRule: use a literal storage destination')
    path = Path(value).expanduser()
    return (path if path.is_absolute() else cwd / path).resolve()


@dataclass(frozen=True)
class ShellInvocation:
    """One decoded shell command and the directory its preceding commands select."""

    words: tuple[str, ...]
    cwd: Path

    @property
    def program(self) -> str:
        return Path(self.words[0]).name

    @property
    def args(self) -> list[str]:
        return list(self.words[1:])

    @classmethod
    def parse(cls, command: str, cwd: Path) -> Iterator[ShellInvocation]:
        for words in shell_commands(command):
            while words and '=' in words[0]:
                words.pop(0)
            if not words:
                continue
            if Path(words[0]).name == 'cd' and len(words) > 1:
                target = words[2] if words[1] == '--' and len(words) > 2 else words[1]
                cwd = physical_path(target, cwd)
                continue
            yield cls(tuple(words), cwd)


class GitWriteOperation(StrEnum):
    COMMIT = 'commit'
    PUSH = 'push'


@dataclass(frozen=True)
class GitInvocation:
    args: list[str]
    cwd: Path
    directories: tuple[Path, ...]

    @property
    def writes_source(self) -> bool:
        return bool(self.args) and self.args[0] in GitWriteOperation


def shell_commands(command):
    """Shell words, not matches in quoted code or here-document bodies."""
    body = []
    lines = iter(command.splitlines(keepends=True))
    for line in lines:
        body.append(line)
        lex = shlex.shlex(line, posix=False, punctuation_chars='<>')
        lex.whitespace_split = True
        words = list(lex)
        for index, word in enumerate(words[:-1]):
            if word in ('<<', '<<-'):
                delimiter = shlex.split(words[index + 1])[0]
                for line in lines:
                    if line.rstrip('\r\n').lstrip('\t') == delimiter:
                        break
    lex = shlex.shlex(''.join(body), posix=True, punctuation_chars=';&|><()\n')
    lex.whitespace = ' \t\r'
    lex.whitespace_split = True
    for separator, words in groupby(lex, key=lambda word: bool(word) and set(word) <= set(';&|()\n')):
        if not separator:
            yield list(words)


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


class AddsCheckout(ShellProgram):
    def decode(self, args, cwd):
        if args[:2] != ['worktree', 'add']:
            return ()
        tail = args[2:]
        while tail and tail[0].startswith('-'):
            tail = tail[2:] if tail[0] in ('-b', '-B', '--reason') else tail[1:]
        if not tail:
            raise StorageDenied('StorageRule: worktree destination is missing')
        return (CheckoutStorageEffect(physical_path(tail[0], cwd)),)


class RemovesCheckout(ShellProgram):
    def decode(self, args, cwd):
        paths = [p for p in args[2:] if not p.startswith('-')] if args[:2] == ['worktree', 'remove'] else []
        return (RemoveCheckoutStorageEffect(physical_path(paths[0], cwd)),) if paths else ()


class WritesWorkingTree(ShellProgram):
    verbs = (('switch',), ('checkout',), (GitWriteOperation.COMMIT,),
             ('merge',), ('reset',), ('stash',))

    def decode(self, args: list[str], cwd: Path) -> tuple[StorageEffect, ...]:
        return (WriteStorageEffect(cwd),) if self.matches(args, self.verbs) else ()


class GitProgram(ForbidsCommands, AddsCheckout, RemovesCheckout, WritesWorkingTree):
    names = frozenset({'git'})
    forbidden = (('clone',), ('worktree', 'move'))
    reason = 'git clone and git worktree move are forbidden; add and remove worktrees instead'

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
        directories = [cwd]
        for option, value in options:
            if option in cls.directory_options:
                path = physical_path(value, cwd)
                directories.append(path)
                if option == cls.chdir_option:
                    cwd = path
        return GitInvocation(remaining, cwd, tuple(directories))

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
