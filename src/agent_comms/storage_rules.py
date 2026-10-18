"""Configured storage limits over current filesystem and Git worktrees."""
from __future__ import annotations

import json
import math
import re
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Annotated, ClassVar, TYPE_CHECKING

from .field_codec import FieldCodec, PathText
from .native_tool_call import SelectedToolDenied
if TYPE_CHECKING:
    from .threads import Thread

StoragePath = Annotated[Path, PathText]


@dataclass(frozen=True)
class StorageRoots:
    scratch: StoragePath

    def __post_init__(self):
        if not self.scratch.is_absolute():
            raise ValueError("Storage scratch requires an absolute path.")


@dataclass(frozen=True)
class StorageRule:
    floor_gib: float = 5.0  # under this nothing that allocates may run
    cleanup_gib: float = 30.0  # under this a cleanup owner is due; it does not stop builds
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
        """The checkout thread ``name`` holds for one ordered problem."""
        return self.checkout_base.resolve() / f'{name}--{problem}'

    def owns_checkout(self, thread: Thread, path: Path) -> bool:
        problem = path.name.removeprefix(f'{thread.name}--')
        return (re.fullmatch(r'P[1-9][0-9]*', problem) is not None
                and path == self.order_checkout(thread.name, problem))

    def checkouts_of(self, thread: Thread) -> tuple[Path, ...]:
        """Read each repository now; directories and names do not register ownership."""
        home = self.checkout_base / thread.name
        home = (home if home.exists() else Path(thread.worktree)).expanduser().resolve()
        ordered = self.order_checkout(thread.name, 'P*')
        candidates = (home, *(path for path in ordered.parent.glob(ordered.name)
                              if path.is_dir() and self.owns_checkout(thread, path)))
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
                             if path.is_dir() and self.owns_checkout(thread, path))
        return (home, *sorted(checkouts - {home}))

    def deny(self, reason):
        raise SelectedToolDenied(f'StorageRule: {reason}')

    def free_gib(self):
        return shutil.disk_usage(self.volume).free / 2**30

    def require_quota(self, thread: Thread, *proposed: Path):
        if UnitStorage.read(self, thread, *proposed).over_checkouts:
            self.deny(f'at most {self.max_checkouts} checkouts besides the home checkout')

    def require_allocation(self, thread: Thread, *proposed: Path):
        if self.free_gib() < self.floor_gib:
            self.deny(f'allocation requires {self.floor_gib:g} GiB free on {self.volume}')
        self.require_quota(thread, *proposed)

    def require_path(self, thread: Thread, path):
        if '.artifacts' in path.parts:
            self.deny('writes inside .artifacts are forbidden')
        if path.is_relative_to(self.scratch_base) and not path.is_relative_to(thread.storage_roots.scratch):
            self.deny(f'foreign scratch destination {path}; registered scratch is {thread.storage_roots.scratch}')
        base = self.checkout_base.resolve()
        if path.is_relative_to(base) and path != base:
            checkout = base / path.relative_to(base).parts[0]
            if not (checkout.name == thread.name or self.owns_checkout(thread, checkout)):
                self.deny(f'{checkout} is not a checkout of {thread.name}')

    def render(self, roots):
        return (f'StorageRule: scratch {roots.scratch}; '
                f'{self.max_checkouts} extra checkouts; allocation floor {self.floor_gib:g} GiB. '
                'No clones, virtualenv creation, uv sync or .artifacts writes.')

    def order_text(self, unit: str, pythons: Sequence[str]) -> str:
        """The rule as an order states it to ``unit``."""
        return (f'Storage (enforced, automatic strike): your checkouts are {self.checkout_base}/{unit}--P<n>, '
                f'at most {self.max_checkouts} besides your home checkout {self.checkout_base}/{unit}, removed '
                f'with git worktree remove in the same order that created them; your only scratch dir is '
                f'{self.scratch_base}/{unit}/, capped at {self.scratch_cap_gib:g} GB together with your checkouts. '
                f'Run tests with PYTHONPATH=src on the shared {" or ".join(pythons)}; '
                'never create a venv, never uv sync, never write .artifacts, never git clone. '
                f'Below {self.floor_gib:g} GB free on {self.volume}, no worktree, venv, build, install or recording.')


@dataclass(frozen=True)
class UnitStorage:
    """One unit's checkouts and its volume's free space, derived on read; its StorageRule judges both."""

    rule: StorageRule
    unit: str
    checkouts: tuple[Path, ...]
    free_gib: float

    @classmethod
    def read(cls, rule: StorageRule, thread: Thread, *proposed: Path) -> UnitStorage:
        checkouts = tuple(dict.fromkeys((*rule.checkouts_of(thread), *proposed)))
        return cls(rule, thread.name, checkouts, rule.free_gib())

    @property
    def over_checkouts(self) -> bool:
        return len(self.checkouts) > self.rule.max_checkouts + 1

    @property
    def cleanup_due(self) -> bool:
        return self.free_gib < self.rule.cleanup_gib

    @property
    def below_floor(self) -> bool:
        return self.free_gib < self.rule.floor_gib


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


class StorageEffect:
    """Preflight one actual operation; completion stores no filesystem facts."""


@dataclass(frozen=True)
class WriteStorageEffect(StorageEffect):
    path: Path

    def check(self, rule, thread):
        rule.require_path(thread, self.path)


@dataclass(frozen=True)
class ForbiddenStorageEffect(StorageEffect):
    reason: str

    def check(self, rule, thread):
        rule.deny(self.reason)


@dataclass(frozen=True)
class AllocateStorageEffect(StorageEffect):
    def check(self, rule, thread):
        rule.require_allocation(thread)


@dataclass(frozen=True)
class CheckoutStorageEffect(AllocateStorageEffect):
    path: Path

    def check(self, rule, thread):
        rule.require_path(thread, self.path)
        if not rule.owns_checkout(thread, self.path):
            destination = rule.order_checkout(thread.name, "P<number>")
            rule.deny(f'checkout destination must be {destination}')
        rule.require_allocation(thread, self.path)


@dataclass(frozen=True)
class RemoveCheckoutStorageEffect(StorageEffect):
    path: Path

    def check(self, rule, thread):
        if self.path not in rule.checkouts_of(thread)[1:]:
            rule.deny('only a current extra checkout can be removed')


def physical_path(value, cwd):
    if '$' in value or '`' in value:
        raise SelectedToolDenied('StorageRule: use a literal storage destination')
    path = Path(value).expanduser()
    return (path if path.is_absolute() else cwd / path).resolve()


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
            raise SelectedToolDenied('StorageRule: worktree destination is missing')
        return (CheckoutStorageEffect(physical_path(tail[0], cwd)),)


class RemovesCheckout(ShellProgram):
    def decode(self, args, cwd):
        paths = [p for p in args[2:] if not p.startswith('-')] if args[:2] == ['worktree', 'remove'] else []
        return (RemoveCheckoutStorageEffect(physical_path(paths[0], cwd)),) if paths else ()


class WritesWorkingTree(ShellProgram):
    verbs = (('switch',), ('checkout',), ('commit',), ('merge',), ('reset',), ('stash',))

    def decode(self, args: list[str], cwd: Path) -> tuple[StorageEffect, ...]:
        return (WriteStorageEffect(cwd),) if self.matches(args, self.verbs) else ()


class GitProgram(ForbidsCommands, AddsCheckout, RemovesCheckout, WritesWorkingTree):
    names = frozenset({'git'})
    forbidden = (('clone',), ('worktree', 'move'))
    reason = 'git clone and git worktree move are forbidden; add and remove worktrees instead'

    def effects(self, args, cwd):
        while args and args[0] in ('-C', '-c', '--git-dir', '--work-tree'):
            if len(args) < 2:
                raise SelectedToolDenied('StorageRule: incomplete git option')
            if args[0] == '-C':
                cwd = physical_path(args[1], cwd)
            args = args[2:]
        return super().effects(args, cwd)


class PythonProgram(ForbidsCommands, AllocatesOnCommands):
    names = frozenset({'python'})
    versioned = True
    forbidden = (('-m', 'venv'),)
    prefixes = (('-m', 'pip', 'install'), ('build_runtime.py',))

    def effects(self, args, cwd):
        args = [Path(args[0]).name, *args[1:]] if args and not args[0].startswith('-') else args
        return super().effects(args, cwd)


class UvProgram(ForbidsCommands, AllocatesOnCommands):
    names = frozenset({'uv'})
    forbidden = (('venv',), ('sync',))
    prefixes = (('pip', 'install'),)


class MakeEnvironmentProgram(ForbidsCommands):
    names = frozenset({'virtualenv'})


class DiskProgram(AllocatesOnCommands):
    names = frozenset({'ffmpeg', 'recordmydesktop', 'build_runtime.py'})


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
    for words in shell_commands(command):
        while words and '=' in words[0]:
            words.pop(0)
        if not words:
            continue
        program, args = Path(words[0]).name, words[1:]
        if program == 'cd' and args:
            cwd = physical_path(args[0], cwd)
            continue
        for index, word in enumerate(args[:-1]):
            if word in ('>', '>>'):
                effects.append(WriteStorageEffect(physical_path(args[index + 1], cwd)))
        effects.extend(ShellProgram.named(program).effects(args, cwd))
    return tuple(effects)
