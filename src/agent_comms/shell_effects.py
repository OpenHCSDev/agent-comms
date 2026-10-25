"""Shell command effects, decoded below storage enforcement and order consumers."""
from __future__ import annotations

import getopt
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .errors import ToolRefusedError
from .native_build_inputs import GitLaunch
from .repository_name import RepositoryName
from .shell_commands import ShellCommand
from .storage_room import AllocationRoomWork, BuildRoomWork, TestGateRoomWork
from .tool_denial import SelectedToolDenied

if TYPE_CHECKING:
    from .storage_rules import StorageProjection


class ShellProgram:
    names: ClassVar[frozenset[str]] = frozenset()
    programs: ClassVar[dict[str, type[ShellProgram]]] = {}
    versioned = False

    @classmethod
    def executable_names(cls) -> frozenset[str]:
        """Names declared here or inherited from an executable capability such as GitLaunch."""
        return cls.names | frozenset(owner.program for owner in cls.__mro__
                                     if 'program' in owner.__dict__)

    def __init_subclass__(cls):
        super().__init_subclass__()
        for name in cls.__dict__.get('names', ()):
            ShellProgram.programs[name] = cls
        for name in cls.executable_names():
            ShellProgram.programs.setdefault(name, cls)

    @classmethod
    def named(cls, name):
        kind = cls.programs.get(name if name in cls.programs else re.sub(r'\d+(?:\.\d+)*$', '', name))
        return (kind if kind and (kind.versioned or name in kind.executable_names()) else cls)()

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

    def require_order_check(self) -> None:
        """Ordinary storage effects remain valid in an order check."""

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

    def require_order_check(self) -> None:
        raise ValueError(
            "Order checks never launch pytest; give the integrator the test paths "
            "and the unresolved risk that requires the run."
        )


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
                         self.matches((name, *args), owner.commands) for name in self.executable_names()))


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


class GitProgram(GitLaunch, ForbidsCommands, WritesWorkingTree):

    forbidden = (('clone',), ('worktree', 'add'), ('worktree', 'move'), ('worktree', 'remove'))
    reason = ('git clone and git worktree add, move and remove are forbidden; run agent-comms '
              'checkout add PROBLEM or checkout remove PROBLEM, which own the pinned submodules')

    @classmethod
    def invocation(cls, args: list[str], cwd: Path) -> GitInvocation:
        try:
            selected = cls.options(args)
        except getopt.GetoptError as error:
            raise SelectedToolDenied(
                f'StorageRule: incomplete or unknown git option: {error}') from error
        for option, value in selected.values:
            if option in cls.directory_options:
                path = physical_path(value, cwd)
                if option == cls.chdir_option:
                    cwd = path
        return GitInvocation(selected.arguments, cwd)

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
