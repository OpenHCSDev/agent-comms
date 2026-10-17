"""Configured native storage rules; filesystem ownership is registry data."""
from __future__ import annotations

import json
import math
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, replace
from itertools import groupby
from pathlib import Path
from typing import Annotated, ClassVar, TYPE_CHECKING

from .field_codec import FieldCodec, PathText
from .native_tool_call import SelectedToolDenied
from .thread_identity import ThreadIncarnation

if TYPE_CHECKING:
    from .registration import Registration

StoragePath = Annotated[Path, PathText]


@dataclass(frozen=True)
class StorageRoots:
    checkouts: tuple[StoragePath, ...]
    scratch: StoragePath

    def __post_init__(self):
        if not self.checkouts or any(not p.is_absolute() for p in (*self.checkouts, self.scratch)):
            raise ValueError("Storage roots require absolute registered paths.")

    def adding(self, path: Path):
        return replace(self, checkouts=tuple(dict.fromkeys((*self.checkouts, path))))

    def removing(self, paths):
        return replace(self, checkouts=tuple(p for p in self.checkouts if p not in paths))

    def usage(self):
        paths = tuple(dict.fromkeys(p for p in (*self.checkouts, self.scratch) if p.exists()))
        if not paths:
            return 0
        result = subprocess.run(('du', '-sbc', '--', *(str(p) for p in paths)),
                                capture_output=True, text=True, check=True, timeout=30)
        return int(result.stdout.splitlines()[-1].split()[0])


@dataclass(frozen=True)
class StorageRule:
    floor_gib: float = 30.0
    unit_cap_gib: float = 3.0
    max_checkouts: int = 2
    volume: StoragePath = Path.home()
    scratch_base: StoragePath = Path.home() / '.cache' / 'agent-scratch'
    checkout_base: StoragePath = Path.home() / 'wt'

    def __post_init__(self):
        if not all(math.isfinite(n) for n in (self.floor_gib, self.unit_cap_gib)):
            raise ValueError("Storage limits must be finite.")
        if min(self.floor_gib, self.unit_cap_gib, self.max_checkouts) < 0:
            raise ValueError("Storage limits cannot be negative.")

    @classmethod
    def load(cls, root: Path):
        path = root / 'storage.json'
        return FieldCodec.decode(cls, json.loads(path.read_text()) if path.exists() else {})

    def initial_roots(self, name, worktree):
        return StorageRoots((Path(worktree).expanduser().resolve(),), self.scratch_base / name)

    def deny(self, reason):
        raise SelectedToolDenied(f'StorageRule: {reason}')

    def free_gib(self):
        return shutil.disk_usage(self.volume).free / 2**30

    def require_quota(self, roots):
        if len(roots.checkouts) > self.max_checkouts + 1:
            self.deny(f'at most {self.max_checkouts} checkouts besides the home checkout')
        if roots.usage() > self.unit_cap_gib * 2**30:
            self.deny(f'checkout and scratch usage exceeds {self.unit_cap_gib:g} GiB')

    def require_allocation(self, roots):
        if self.free_gib() < self.floor_gib:
            self.deny(f'allocation requires {self.floor_gib:g} GiB free on {self.volume}')
        self.require_quota(roots)

    def require_path(self, roots, path):
        if '.artifacts' in path.parts:
            self.deny('writes inside .artifacts are forbidden')
        if path.is_relative_to(self.scratch_base) and not any(path.is_relative_to(p) for p in (roots.scratch, *roots.checkouts)):
            self.deny(f'foreign scratch destination {path}; registered scratch is {roots.scratch}')

    def render(self, roots):
        return (f'StorageRule: scratch {roots.scratch}; {self.unit_cap_gib:g} GiB combined cap; '
                f'{self.max_checkouts} extra checkouts; allocation floor {self.floor_gib:g} GiB. '
                'No clones, virtualenv creation, uv sync or .artifacts writes.')


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
    def removed_checkouts(self):
        return ()


@dataclass(frozen=True)
class WriteStorageEffect(StorageEffect):
    path: Path

    def check(self, rule, roots):
        rule.require_path(roots, self.path)
        return roots


@dataclass(frozen=True)
class ForbiddenStorageEffect(StorageEffect):
    reason: str

    def check(self, rule, roots):
        rule.deny(self.reason)


@dataclass(frozen=True)
class AllocateStorageEffect(StorageEffect):
    def check(self, rule, roots):
        rule.require_allocation(roots)
        return roots


@dataclass(frozen=True)
class CheckoutStorageEffect(AllocateStorageEffect):
    path: Path

    def check(self, rule, roots):
        rule.require_path(roots, self.path)
        if self.path.parent != rule.checkout_base.resolve() or not re.fullmatch(re.escape(roots.scratch.name) + r'--P[1-9][0-9]*', self.path.name):
            rule.deny(f'checkout destination must be {rule.checkout_base}/{roots.scratch.name}--P<number>')
        proposed = roots.adding(self.path)
        rule.require_allocation(proposed)
        return proposed


@dataclass(frozen=True)
class RemoveCheckoutStorageEffect(StorageEffect):
    path: Path

    def check(self, rule, roots):
        if self.path not in roots.checkouts[1:]:
            rule.deny('only a registered extra checkout can be removed')
        return roots

    def removed_checkouts(self):
        return (self.path,)


@dataclass(frozen=True)
class StorageDecision:
    actor: ThreadIncarnation
    removed: tuple[Path, ...]
    registry: Registration

    def complete(self):
        if not self.removed:
            return
        gone = tuple(path for path in self.removed if not path.exists())
        if not gone:
            return
        with self.registry.store.editing() as edit:
            current = edit.document.require(self.actor.name)
            if current.incarnation != self.actor:
                raise SelectedToolDenied('StorageRule: checkout removal owner changed')
            proposed = current.storage_roots.removing(gone)
            if proposed != current.storage_roots:
                edit.document.threads[current.name] = replace(current, storage_roots=proposed)
                edit.commit()


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


class GitProgram(ForbidsCommands, AddsCheckout, RemovesCheckout):
    names = frozenset({'git'})
    forbidden = (('clone',),)
    reason = 'git clone is forbidden; use an existing repository'

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
