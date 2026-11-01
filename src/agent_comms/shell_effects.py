"""Shell command effects, decoded below storage enforcement and order consumers."""
from __future__ import annotations

import getopt
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from agent_comms_hooks import ToolRefusedError
from agent_comms_hooks.shell_commands import ShellCommand

from .native_build_inputs import GitLaunch, NamedProgram
from .repo import Checkout, CheckoutStart, Repo
from .storage_room import AllocationRoomWork, BuildRoomWork, RoomWork, TestGateRoomWork
from .tool_denial import SelectedToolDenied

if TYPE_CHECKING:
    from .storage_rules import StorageProjection, UnitStorage


class ShellProgram(NamedProgram):
    programs: ClassVar[dict[str, type[ShellProgram]]] = {}

    @classmethod
    def register_names(cls, names: frozenset[str]) -> None:
        for name in names:
            ShellProgram.programs[name] = cls
        for name in cls.names:
            ShellProgram.programs.setdefault(name, cls)

    @classmethod
    def named(cls, name: str) -> ShellProgram:
        normalized = name if name in cls.programs else re.sub(r'\d+(?:\.\d+)*$', '', name)
        kind = cls.programs.get(normalized, cls)
        return (kind if kind.accepts(name) else cls)()

    @classmethod
    def accepts(cls, name: str) -> bool:
        return name in cls.names

    @staticmethod
    def matches(args: Sequence[str], prefixes: Sequence[tuple[str, ...]]) -> bool:
        return any(tuple(args[:len(prefix)]) == prefix for prefix in prefixes)

    def following_directory(self, args: list[str], cwd: Path) -> Path:
        return cwd

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
        projection.require_write(self.path)


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
    """Shared checkout ownership and free-space admission."""

    path: Path
    repository: Repo

    def project(self, storage: UnitStorage) -> UnitStorage:
        return storage.adding(Checkout(self.path))

    def check(self, projection: StorageProjection) -> None:
        rule, thread = projection.rule, projection.thread
        rule.require_destination(thread, self.path)
        if not rule.owns_checkout(thread, self.path, self.repository.github, projection.reads):
            destination = rule.order_checkout(rule.roots_of(thread), "P<number>")
            rule.deny(f'checkout destination must be {destination}')
        projection.storage = self.project(projection.storage)
        super().check(projection)


@dataclass(frozen=True)
class CreateCheckoutStorageEffect(CheckoutStorageEffect):
    start: CheckoutStart


@dataclass(frozen=True)
class RemoveCheckoutStorageEffect(StorageEffect):
    path: Path

    def check(self, projection: StorageProjection) -> None:
        if self.path == projection.thread.project_path:
            projection.rule.deny(f'{self.path} is where {projection.thread.name} runs; choose another existing project first: agent-comms set-project {projection.thread.name} <existing-project>')
        projection.storage = projection.storage.removing(Checkout(self.path))


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
            invocation = cls(decoded.words, cwd, redirections=decoded.redirections)
            yield invocation
            cwd = ShellProgram.named(decoded.program).following_directory(decoded.args, cwd)


class ChangeDirectoryProgram(ShellProgram, names=frozenset({'cd'})):
    def following_directory(self, args: list[str], cwd: Path) -> Path:
        try:
            _, targets = getopt.getopt(args, '')
        except getopt.GetoptError as error:
            raise StorageDenied(f'StorageRule: unknown cd option: {error}') from error
        if targets:
            cwd = physical_path(targets[0], cwd)
        return cwd


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
        return super().effects(args, cwd) + BuildsOnCommands.measured(args, self.names)


class TestsOnCommands(MeasuredCommands):
    effect: ClassVar[type[AllocateStorageEffect]] = TestGateStorageEffect
    commands: ClassVar[tuple[tuple[str, ...], ...]] = (('pytest',), ('py.test',), ('python', '-m', 'pytest'),
                ('python', 'test_gate.py'))

    def effects(self, args: list[str], cwd: Path) -> tuple[StorageEffect, ...]:
        return super().effects(args, cwd) + TestsOnCommands.measured(args, self.names)


class WritesTargets(ShellProgram):
    """The declaring CLI owns option arity; only decoded operands become paths."""

    short_options: ClassVar[str] = ''
    long_options: ClassVar[tuple[str, ...]] = ('help', 'version')

    def destinations(self, options: list[tuple[str, str]], arguments: list[str]) -> tuple[str, ...]:
        return tuple(arguments)

    def effects(self, args: list[str], cwd: Path) -> tuple[StorageEffect, ...]:
        try:
            options, arguments = getopt.gnu_getopt(args, self.short_options, self.long_options)
        except getopt.GetoptError as error:
            raise StorageDenied(f'StorageRule: incomplete or unknown file-program option: {error}') from error
        paths = () if any(option in ('--help', '--version') for option, _ in options) else self.destinations(options, arguments)
        return super().effects(args, cwd) + tuple(
            WriteStorageEffect(physical_path(path, cwd)) for path in paths)


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
    def git_invocation(cls, args: list[str], cwd: Path) -> GitInvocation:
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
        invocation = self.git_invocation(args, cwd)
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
        '--help', '--help-env', '--help-xoptions', '--help-all', '--version', '--check-hash-based-pycs=',
    )
    execution_options: ClassVar[frozenset[str]] = frozenset({'-m', '-c'})

    def effects(self, args: list[str], cwd: Path) -> tuple[StorageEffect, ...]:
        end = next((index for index, word in enumerate(args) if word in self.execution_options), len(args))
        try:
            _, arguments = getopt.getopt(
                args[:end], self.interpreter_options,
                tuple(option.removeprefix('--') for option in self.long_options),
            )
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


class MakeDirectoryProgram(WritesTargets, names=frozenset({'mkdir'})):
    short_options = 'm:pvZ'
    long_options = (*WritesTargets.long_options, 'mode=', 'parents', 'verbose', 'context=?')


class TouchProgram(WritesTargets, names=frozenset({'touch'})):
    short_options = 'acd:fhmr:t:'
    long_options = (*WritesTargets.long_options, 'no-create', 'date=', 'no-dereference', 'reference=', 'time=')

    def destinations(self, options: list[tuple[str, str]], arguments: list[str]) -> tuple[str, ...]:
        # '-' names the selected stdout descriptor, not a file named '-'.
        return tuple(path for path in arguments if path != '-')


class TeeProgram(WritesTargets, names=frozenset({'tee'})):
    short_options = 'aip'
    long_options = (*WritesTargets.long_options, 'append', 'ignore-interrupts', 'output-error=?')


class CopyProgram(WritesTargets):
    """Copy-like CLIs share the final destination operand, not their option meanings."""

    def destinations(self, options: list[tuple[str, str]], arguments: list[str]) -> tuple[str, ...]:
        return tuple(arguments[-1:]) if len(arguments) >= 2 else ()


class TargetDirectoryCopyProgram(CopyProgram):
    short_options = 't:S:'
    long_options = (*CopyProgram.long_options, 'target-directory=', 'suffix=')

    def destinations(self, options: list[tuple[str, str]], arguments: list[str]) -> tuple[str, ...]:
        directories = tuple(value for option, value in options if option in ('-t', '--target-directory'))
        return directories or super().destinations(options, arguments)


class CopyFilesProgram(TargetDirectoryCopyProgram, names=frozenset({'cp'})):
    short_options = TargetDirectoryCopyProgram.short_options + 'abdfiHLPlnpRrsTuvxZ'
    long_options = (*TargetDirectoryCopyProgram.long_options, 'archive', 'attributes-only', 'backup=?', 'copy-contents',
                    'no-dereference', 'debug', 'force', 'interactive', 'dereference', 'keep-directory-symlink',
                    'link', 'no-clobber', 'preserve=?', 'no-preserve=', 'parents', 'recursive', 'reflink=?',
                    'remove-destination', 'sparse=', 'strip-trailing-slashes', 'symbolic-link',
                    'no-target-directory', 'update=?', 'verbose', 'one-file-system', 'context=?')


class MoveProgram(TargetDirectoryCopyProgram, names=frozenset({'mv'})):
    short_options = TargetDirectoryCopyProgram.short_options + 'bfinTuvZ'
    long_options = (*TargetDirectoryCopyProgram.long_options, 'backup=?', 'debug', 'exchange', 'force', 'interactive',
                    'no-clobber', 'no-copy', 'strip-trailing-slashes', 'no-target-directory',
                    'update=?', 'verbose', 'context')

    def destinations(self, options: list[tuple[str, str]], arguments: list[str]) -> tuple[str, ...]:
        # Moving also removes source directory entries; destination-only admission hid that write.
        return tuple(dict.fromkeys((*super().destinations(options, arguments), *arguments)))


class InstallFilesProgram(TargetDirectoryCopyProgram, names=frozenset({'install'})):
    short_options = TargetDirectoryCopyProgram.short_options + 'bcCdDg:m:o:psTvZ'
    long_options = (*TargetDirectoryCopyProgram.long_options, 'backup=?', 'compare', 'directory', 'debug', 'group=',
                    'mode=', 'owner=', 'preserve-timestamps', 'strip', 'strip-program=',
                    'no-target-directory', 'verbose', 'preserve-context', 'context=?')

    def destinations(self, options: list[tuple[str, str]], arguments: list[str]) -> tuple[str, ...]:
        if any(option in ('-d', '--directory') for option, _ in options):
            return tuple(arguments)
        return super().destinations(options, arguments)


class LinkProgram(TargetDirectoryCopyProgram, names=frozenset({'ln'})):
    short_options = TargetDirectoryCopyProgram.short_options + 'bdFfiLnPrsTv'
    long_options = (*TargetDirectoryCopyProgram.long_options, 'backup=?', 'directory', 'force', 'interactive', 'logical',
                    'no-dereference', 'physical', 'relative', 'symbolic', 'no-target-directory', 'verbose')

    def destinations(self, options: list[tuple[str, str]], arguments: list[str]) -> tuple[str, ...]:
        selected = super().destinations(options, arguments)
        return selected or (('.',) if len(arguments) == 1 else ())


class RsyncProgram(CopyProgram, names=frozenset({'rsync'})):
    # Remote/native expansion is not inferred. Unknown options are explicit refusals.
    short_options = '0468ACDEFGHIJKLNOPRSUVWXM:B:T:e:f:abcdghiklmnopqrstuvxyz@:'
    long_options = (*WritesTargets.long_options, 'verbose', 'quiet', 'archive', 'recursive', 'relative',
                    'checksum', 'links', 'copy-links', 'perms', 'owner', 'group', 'times', 'dry-run',
                    'delete', 'delete-before', 'delete-during', 'delete-after', 'progress', 'stats',
                    'human-readable', 'itemize-changes', 'compress', 'one-file-system', 'inplace',
                    'partial', 'numeric-ids', 'ignore-existing', 'size-only', 'remove-source-files',
                    'info=', 'debug=', 'stderr=', 'suffix=', 'backup-dir=', 'chmod=', 'chown=',
                    'block-size=', 'rsh=', 'rsync-path=', 'timeout=', 'contimeout=', 'modify-window=',
                    'temp-dir=', 'partial-dir=', 'compare-dest=', 'copy-dest=', 'link-dest=',
                    'filter=', 'exclude=', 'exclude-from=', 'include=', 'include-from=', 'files-from=',
                    'out-format=', 'log-file=', 'log-file-format=', 'bwlimit=', 'max-size=', 'min-size=',
                    'write-batch=', 'only-write-batch=', 'read-batch=', 'remote-option=')
    receiver_writes: ClassVar[frozenset[str]] = frozenset({
        '-T', '--temp-dir', '--partial-dir', '--backup-dir'})
    auxiliary_writes: ClassVar[frozenset[str]] = receiver_writes | frozenset({
        '--log-file', '--write-batch', '--only-write-batch'})

    def destinations(self, options: list[tuple[str, str]], arguments: list[str]) -> tuple[str, ...]:
        paths = super().destinations(options, arguments)
        for option, value in options:
            if option in self.receiver_writes and not Path(value).is_absolute():
                raise StorageDenied('StorageRule: use an absolute rsync receiver-side destination')
        paths += tuple(value for option, value in options if option in self.auxiliary_writes)
        if any(option == '--remove-source-files' for option, _ in options):
            paths += tuple(arguments[:-1])
        if any(':' in path for path in paths):
            raise StorageDenied('StorageRule: remote rsync destinations require a separately admitted transport')
        return tuple(dict.fromkeys(paths))


def shell_storage_effects(command: str, cwd: Path) -> tuple[StorageEffect, ...]:
    """Decode known shell operations, not arbitrary interpreter programs."""
    effects: list[StorageEffect] = []
    for invocation in ShellInvocation.parse(command, cwd):
        for redirection in invocation.redirections:
            effects.extend(WriteStorageEffect(physical_path(path, invocation.cwd))
                           for path in redirection.written_paths())
        effects.extend(ShellProgram.named(invocation.program).effects(invocation.args, invocation.cwd))
    return tuple(effects)
