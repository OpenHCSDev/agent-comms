"""Shell command effects, decoded below storage enforcement and order consumers."""
from __future__ import annotations

import getopt
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .errors import ToolRefusedError
from .native_build_inputs import Executable, GitLaunch
from .repository_name import RepositoryName
from .shell_commands import ShellCommand
from .storage_room import AllocationRoomWork, BuildRoomWork, RoomWork, TestGateRoomWork
from .tool_denial import SelectedToolDenied

if TYPE_CHECKING:
    from .storage_rules import StorageProjection


class ShellProgram:
    names: ClassVar[frozenset[str]] = frozenset()
    programs: ClassVar[dict[str, type[ShellProgram]]] = {}

    @classmethod
    def executable_names(cls) -> frozenset[str]:
        """Aliases and executable capabilities declare names; consumers derive them."""
        return cls.names | frozenset(
            owner.program for owner in cls.__mro__ if issubclass(owner, Executable)
        )

    def __init_subclass__(cls, *, names: frozenset[str] = frozenset()) -> None:
        super().__init_subclass__()
        cls.names = cls.names | names
        for name in names:
            ShellProgram.programs[name] = cls
        for name in cls.executable_names():
            ShellProgram.programs.setdefault(name, cls)

    @classmethod
    def named(cls, name: str) -> ShellProgram:
        normalized = name if name in cls.programs else re.sub(r'\d+(?:\.\d+)*$', '', name)
        kind = cls.programs.get(normalized, cls)
        return (kind if kind.accepts(name) else cls)()

    @classmethod
    def accepts(cls, name: str) -> bool:
        return name in cls.executable_names()

    @staticmethod
    def matches(args: Sequence[str], prefixes: Sequence[tuple[str, ...]]) -> bool:
        return any(tuple(args[:len(prefix)]) == prefix for prefix in prefixes)

    def effects(self, args: list[str], cwd: Path) -> tuple[StorageEffect, ...]:
        """Unknown programs have no declared effects; capabilities compose through super()."""
        return ()


class StorageDenied(SelectedToolDenied, ToolRefusedError):  # noqa: N818 - existing nominal refusal
    """The storage preflight refused an operation before it executed."""


class StorageEffect(ABC):
    """Preflight one actual operation and declare its measured work, if any."""
    work: ClassVar[tuple[type[RoomWork], ...]] = ()

    def require_order_check(self) -> None:
        """Ordinary storage effects remain valid in an order check."""

    @abstractmethod
    def check(self, projection: StorageProjection) -> None:
        """Refuse this operation if the thread's storage cannot take it."""


@dataclass(frozen=True)
class WriteStorageEffect(StorageEffect):
    path: Path

    def check(self, projection: StorageProjection) -> None:
        projection.rule.require_path(projection.thread, self.path)


@dataclass(frozen=True)
class ForbiddenStorageEffect(StorageEffect):
    reason: str

    def check(self, projection: StorageProjection) -> None:
        projection.rule.deny(self.reason)


@dataclass(frozen=True)
class AllocateStorageEffect(StorageEffect):
    work: ClassVar[tuple[type[RoomWork], ...]] = (AllocationRoomWork,)

    def check(self, projection: StorageProjection) -> None:
        projection.rule.require_floor()
        projection.storage.require_quota()


class BuildStorageEffect(AllocateStorageEffect):
    work: ClassVar[tuple[type[RoomWork], ...]] = (BuildRoomWork,)


class TestGateStorageEffect(AllocateStorageEffect):
    work: ClassVar[tuple[type[RoomWork], ...]] = (TestGateRoomWork,)

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

    def check(self, projection: StorageProjection) -> None:
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

    def check(self, projection: StorageProjection) -> None:
        if self.path == projection.thread.project_path:
            projection.rule.deny(f'{self.path} is where {projection.thread.name} runs; choose another existing project first: agent-comms set-project {projection.thread.name} <existing-project>')
        projection.storage = projection.storage.removing(self.path)


def physical_path(value: str, cwd: Path) -> Path:
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
    forbidden: ClassVar[tuple[tuple[str, ...], ...]] = ((),)
    reason: ClassVar[str] = 'virtualenv creation and uv sync are forbidden; use the shared environment'

    def effects(self, args: list[str], cwd: Path) -> tuple[StorageEffect, ...]:
        selected = (ForbiddenStorageEffect(self.reason),) if self.matches(args, self.forbidden) else ()
        return super().effects(args, cwd) + selected


class AllocatesOnCommands(ShellProgram):
    prefixes: ClassVar[tuple[tuple[str, ...], ...]] = ((),)

    def effects(self, args: list[str], cwd: Path) -> tuple[StorageEffect, ...]:
        selected = (AllocateStorageEffect(),) if (
            {'--help', '-h'}.isdisjoint(args) and self.matches(args, self.prefixes)
        ) else ()
        return super().effects(args, cwd) + selected


class MeasuredCommands(ShellProgram):
    """A capability's own command/effect declarations, not a second MRO registry."""

    commands: ClassVar[tuple[tuple[str, ...], ...]] = ()
    effect: ClassVar[type[AllocateStorageEffect]] = AllocateStorageEffect

    @classmethod
    def measured(cls, args: list[str], names: frozenset[str]) -> tuple[StorageEffect, ...]:
        if not {'--help', '-h'}.isdisjoint(args):
            return ()
        return (cls.effect(),) if any(
            cls.matches((name, *args), cls.commands) for name in names
        ) else ()


class BuildsOnCommands(MeasuredCommands):
    effect: ClassVar[type[AllocateStorageEffect]] = BuildStorageEffect
    commands: ClassVar[tuple[tuple[str, ...], ...]] = (('build_runtime.py',), ('python', 'build_runtime.py'),
                ('python', '-m', 'hatchling', 'build'), ('python', '-m', 'build'))

    def effects(self, args: list[str], cwd: Path) -> tuple[StorageEffect, ...]:
        return super().effects(args, cwd) + BuildsOnCommands.measured(args, self.executable_names())


class TestsOnCommands(MeasuredCommands):
    effect: ClassVar[type[AllocateStorageEffect]] = TestGateStorageEffect
    commands: ClassVar[tuple[tuple[str, ...], ...]] = (('pytest',), ('py.test',), ('python', '-m', 'pytest'),
                ('python', 'test_gate.py'))

    def effects(self, args: list[str], cwd: Path) -> tuple[StorageEffect, ...]:
        return super().effects(args, cwd) + TestsOnCommands.measured(args, self.executable_names())


class WritesTargets(ShellProgram):
    targets: ClassVar[slice] = slice(None)
    minimum: ClassVar[int] = 1

    def effects(self, args: list[str], cwd: Path) -> tuple[StorageEffect, ...]:
        paths = [arg for arg in args if not arg.startswith('-')]
        selected = tuple(
            WriteStorageEffect(physical_path(path, cwd)) for path in paths[self.targets]
        ) if len(paths) >= self.minimum else ()
        return super().effects(args, cwd) + selected


class WritesWorkingTree(ShellProgram):
    verbs: ClassVar[tuple[tuple[str, ...], ...]] = (
        ('switch',), ('checkout',), ('commit',), ('merge',), ('reset',), ('stash',)
    )

    def effects(self, args: list[str], cwd: Path) -> tuple[StorageEffect, ...]:
        selected = (WriteStorageEffect(cwd),) if self.matches(args, self.verbs) else ()
        return super().effects(args, cwd) + selected


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

    def effects(self, args: list[str], cwd: Path) -> tuple[StorageEffect, ...]:
        invocation = self.invocation(args, cwd)
        return super().effects(invocation.args, invocation.cwd)


class VersionedProgram(ShellProgram):
    """An executable capability whose external command also accepts version suffixes."""

    @classmethod
    def accepts(cls, name: str) -> bool:
        return super().accepts(re.sub(r'\d+(?:\.\d+)*$', '', name))


class PythonProgram(VersionedProgram, ForbidsCommands, BuildsOnCommands, TestsOnCommands,
                    AllocatesOnCommands, names=frozenset({'python'})):
    forbidden = (('-m', 'venv'),)
    prefixes = (('-m', 'pip', 'install'),)

    # The interpreter owns these flags. -m/-c begin its program, whose flags are not ours.
    interpreter_options: ClassVar[str] = 'bBdEhiIOPqRsuUvVW:xX:'
    long_options: ClassVar[tuple[str, ...]] = (
        'help', 'help-env', 'help-xoptions', 'help-all', 'version', 'check-hash-based-pycs=',
    )
    execution_options: ClassVar[frozenset[str]] = frozenset({'-m', '-c'})

    def effects(self, args: list[str], cwd: Path) -> tuple[StorageEffect, ...]:
        end = next((index for index, word in enumerate(args) if word in self.execution_options), len(args))
        try:
            _, arguments = getopt.getopt(args[:end], self.interpreter_options, self.long_options)
        except getopt.GetoptError as error:
            raise SelectedToolDenied(f'StorageRule: incomplete or unknown Python option: {error}') from error
        arguments += args[end:]
        if arguments and not arguments[0].startswith('-'):
            arguments[0] = Path(arguments[0]).name
        return super().effects(arguments, cwd)


class UvProgram(ForbidsCommands, AllocatesOnCommands, names=frozenset({'uv'})):
    forbidden = (('venv',), ('sync',))
    prefixes = (('pip', 'install'),)


class MakeEnvironmentProgram(ForbidsCommands, names=frozenset({'virtualenv'})):
    pass


class DiskProgram(AllocatesOnCommands, names=frozenset({'ffmpeg', 'recordmydesktop'})):
    pass


class BuildProgram(BuildsOnCommands, names=frozenset({'build_runtime.py'})):
    pass


class TestGateProgram(TestsOnCommands, names=frozenset({'pytest', 'py.test'})):
    pass


class PackageProgram(AllocatesOnCommands, names=frozenset({'pip', 'pip3', 'npm', 'pnpm', 'yarn'})):
    prefixes = (('install',), ('ci',), ('add',))


class ProfileProgram(AllocatesOnCommands, names=frozenset({'py-spy'})):
    prefixes = (('record',),)


class FileWriterProgram(WritesTargets, names=frozenset({'mkdir', 'touch', 'tee'})):
    pass


class CopyProgram(WritesTargets, names=frozenset({'cp', 'mv', 'rsync', 'install', 'ln'})):
    targets = slice(-1, None)
    minimum = 2


def shell_storage_effects(command: str, cwd: Path) -> tuple[StorageEffect, ...]:
    """Decode known shell operations, not arbitrary interpreter programs."""
    effects: list[StorageEffect] = []
    for invocation in ShellInvocation.parse(command, cwd):
        args = invocation.args
        for index, word in enumerate(args[:-1]):
            if word in ('>', '>>'):
                effects.append(WriteStorageEffect(physical_path(args[index + 1], invocation.cwd)))
        effects.extend(ShellProgram.named(invocation.program).effects(args, invocation.cwd))
    return tuple(effects)
