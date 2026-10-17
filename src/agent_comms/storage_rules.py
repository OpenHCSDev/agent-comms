"""Configured native storage rules; filesystem ownership is registry data."""
from __future__ import annotations

import json
import math
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, TYPE_CHECKING

from .field_codec import FieldCodec, PathText
from .native_tool_call import SelectedToolDenied
from .thread_identity import ThreadIncarnation

if TYPE_CHECKING:
    from .threads import Thread
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

    def __post_init__(self):
        if not all(math.isfinite(n) for n in (self.floor_gib, self.unit_cap_gib)):
            raise ValueError("Storage limits must be finite.")
        if min(self.floor_gib, self.unit_cap_gib, self.max_checkouts) < 0:
            raise ValueError("Storage limits cannot be negative.")

    @classmethod
    def load(cls, root: Path):
        path = root / 'storage.json'
        return FieldCodec.decode(cls, json.loads(path.read_text()) if path.exists() else {})

    def initial_roots(self, thread: Thread):
        return StorageRoots(tuple(dict.fromkeys(Path(p).resolve() for p in
            (*thread.previous_worktrees, thread.worktree))), self.scratch_base / thread.name)

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
        owned = (roots.scratch, *roots.checkouts)
        if path.is_relative_to(self.scratch_base) and not any(path.is_relative_to(p) for p in owned):
            self.deny(f'foreign scratch destination {path}; registered scratch is {roots.scratch}')

    def render(self, roots):
        return (f'StorageRule: scratch {roots.scratch}; {self.unit_cap_gib:g} GiB combined cap; '
                f'{self.max_checkouts} extra checkouts; allocation floor {self.floor_gib:g} GiB. '
                'No clones, virtualenv creation, uv sync or .artifacts writes.')


class StorageEffect:
    def admit(self, rule, roots):
        return roots

    def removed_checkouts(self):
        return ()


@dataclass(frozen=True)
class WriteStorageEffect(StorageEffect):
    path: Path

    def admit(self, rule, roots):
        rule.require_path(roots, self.path)
        return roots


@dataclass(frozen=True)
class ForbiddenStorageEffect(StorageEffect):
    reason: str

    def admit(self, rule, roots):
        rule.deny(self.reason)


@dataclass(frozen=True)
class AllocateStorageEffect(StorageEffect):
    def admit(self, rule, roots):
        rule.require_allocation(roots)
        return roots


@dataclass(frozen=True)
class CheckoutStorageEffect(AllocateStorageEffect):
    path: Path

    def admit(self, rule, roots):
        rule.require_path(roots, self.path)
        proposed = roots.adding(self.path)
        rule.require_allocation(proposed)
        return proposed


@dataclass(frozen=True)
class RemoveCheckoutStorageEffect(StorageEffect):
    path: Path

    def admit(self, rule, roots):
        if self.path not in roots.checkouts or self.path == roots.checkouts[0]:
            rule.deny('only a registered extra checkout can be removed')
        return roots

    def removed_checkouts(self):
        return (self.path,)


@dataclass(frozen=True)
class StorageDecision:
    actor: ThreadIncarnation
    removed: tuple[Path, ...]
    registry: Registration

    def complete(self, is_error):
        if is_error or not self.removed:
            return
        with self.registry.store.editing() as edit:
            current = edit.document.require(self.actor.name)
            if current.incarnation != self.actor:
                raise SelectedToolDenied('StorageRule: checkout removal owner changed')
            gone = tuple(path for path in self.removed if not path.exists())
            edit.document.threads[current.name] = replace(current,
                storage_roots=current.storage_roots.removing(gone))
            edit.commit()


def physical_path(value, cwd):
    if '$' in value or '`' in value:
        raise SelectedToolDenied('StorageRule: use a literal storage destination')
    path = Path(value).expanduser()
    return (path if path.is_absolute() else cwd / path).resolve()


def shell_commands(command):
    """Shell words, not matches in quoted code or here-document bodies."""
    body = []
    delimiters = []
    for line in command.splitlines(keepends=True):
        if delimiters:
            if line.rstrip('\r\n').lstrip('\t') == delimiters[0]:
                delimiters.pop(0)
            continue
        body.append(line)
        lex = shlex.shlex(line, posix=True, punctuation_chars='<>')
        lex.whitespace_split = True
        words = list(lex)
        for index, word in enumerate(words[:-1]):
            if word in ('<<', '<<-'):
                delimiters.append(words[index + 1])
    lex = shlex.shlex(''.join(body), posix=True, punctuation_chars=';&|><()\n')
    lex.whitespace = ' \t\r'
    lex.whitespace_split = True
    words = []
    for word in lex:
        if word and set(word) <= set(';&|()\n'):
            if words:
                yield words
                words = []
        else:
            words.append(word)
    if words:
        yield words


def shell_storage_effects(command, cwd):
    """Decode known shell storage operations once at the native tool boundary.

    This checks declared shell storage operations, not arbitrary interpreter code.
    Unknown programs retain the existing resource-claim contract.
    """
    effects = []
    for words in shell_commands(command):
        while words and '=' in words[0]:
            words.pop(0)
        if not words:
            continue
        program = Path(words[0]).name
        args = words[1:]
        if program == 'cd' and args:
            cwd = physical_path(args[0], cwd)
            continue
        for index, word in enumerate(args[:-1]):
            if word in ('>', '>>'):
                effects.append(WriteStorageEffect(physical_path(args[index + 1], cwd)))
        if program == 'git':
            git_cwd = cwd
            while args and args[0] in ('-C', '-c', '--git-dir', '--work-tree'):
                if len(args) < 2:
                    raise SelectedToolDenied('StorageRule: incomplete git option')
                if args[0] == '-C':
                    git_cwd = physical_path(args[1], git_cwd)
                args = args[2:]
            if args[:1] == ['clone']:
                effects.append(ForbiddenStorageEffect('git clone is forbidden; use an existing repository'))
            elif args[:2] == ['worktree', 'add']:
                tail = args[2:]
                while tail and tail[0].startswith('-'):
                    tail = tail[2:] if tail[0] in ('-b', '-B', '--reason') else tail[1:]
                if not tail:
                    raise SelectedToolDenied('StorageRule: worktree destination is missing')
                effects.append(CheckoutStorageEffect(physical_path(tail[0], git_cwd)))
            elif args[:2] == ['worktree', 'remove']:
                paths = [p for p in args[2:] if not p.startswith('-')]
                if paths:
                    effects.append(RemoveCheckoutStorageEffect(physical_path(paths[0], git_cwd)))
        python = re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', program)
        if (program == 'uv' and args[:1] in (['venv'], ['sync'])) or program == 'virtualenv' or (python and args[:2] == ['-m', 'venv']):
            effects.append(ForbiddenStorageEffect('virtualenv creation and uv sync are forbidden; use the shared environment'))
        allocator = (program in ('ffmpeg', 'recordmydesktop') or
            program == 'py-spy' and args[:1] == ['record'] or
            program in ('pip', 'pip3', 'npm', 'pnpm', 'yarn') and args[:1] in (['install'], ['ci'], ['add']) or
            program == 'uv' and args[:2] == ['pip', 'install'] or
            python and args[:3] == ['-m', 'pip', 'install'] or
            program == 'build_runtime.py' or python and args and Path(args[0]).name == 'build_runtime.py')
        if allocator:
            effects.append(AllocateStorageEffect())
        targets = []
        positional = [arg for arg in args if not arg.startswith('-')]
        if program in ('mkdir', 'touch', 'tee'):
            targets = positional
        elif program in ('cp', 'mv', 'rsync', 'install', 'ln') and len(positional) >= 2:
            targets = positional[-1:]
        for target in targets:
            effects.append(WriteStorageEffect(physical_path(target, cwd)))
    return tuple(effects)
