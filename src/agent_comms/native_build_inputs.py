"""The source inventory and pinned identity of a native package build."""

from __future__ import annotations

import asyncio
import getopt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from contextvars import Context, ContextVar
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from tempfile import TemporaryFile
from time import monotonic
from typing import BinaryIO, ClassVar, TextIO, cast

INPUTS_PREFIX = "# agent-comms-native-inputs-v1 "
PREPARATION_SCHEME = "sealed-native-store-v1"


@dataclass(frozen=True)
class GitOptions:
    values: list[tuple[str, str]]
    arguments: list[str]


class NamedProgram:
    """Program capabilities compose their declared names before registration."""

    names: ClassVar[frozenset[str]] = frozenset()

    def __init_subclass__(cls, *, names: frozenset[str] = frozenset()) -> None:
        cls.names = cls.names | names
        super().__init_subclass__()
        cls.register_names(names)

    @classmethod
    def register_names(cls, names: frozenset[str]) -> None:
        """Launch-only programs need no shell decoder registration."""


class Executable(NamedProgram):
    """A child-launch capability declares its executable name."""

    program: ClassVar[str]

    def __init_subclass__(cls, *, names: frozenset[str] = frozenset()) -> None:
        cls.names = cls.names | frozenset((cls.program,))
        super().__init_subclass__(names=names)


class GitAcquisitionCancelled(asyncio.CancelledError):
    """Shutdown interrupted an explicitly cancellable Git acquisition, not a Git failure."""


class GitOpening:
    """Stdlib bootstrap constructor; Core binds identity ownership in its work context."""

    @contextmanager
    def opening(
        self, command: tuple[str, ...], *, env: Mapping[str, str],
        stdin: int | BinaryIO | TextIO | None,
        stdout: int | BinaryIO | TextIO | None,
        stderr: int | BinaryIO | TextIO | None,
    ) -> Iterator[subprocess.Popen[bytes]]:
        with subprocess.Popen(command, env=env, stdin=stdin, stdout=stdout, stderr=stderr) as process:
            try:
                yield process
            except BaseException:
                process.kill()
                process.wait()
                raise

    @contextmanager
    def bound(self) -> Iterator[None]:
        token = _git_opening.set(self)
        try:
            yield
        finally:
            _git_opening.reset(token)

    @contextmanager
    def acquiring(self) -> Iterator[None]:
        """Standalone construction has no connector shutdown to borrow."""
        yield

    def enter_phase(self) -> None:
        """A standalone operation has no connector phase gate."""


_git_opening: ContextVar[GitOpening] = ContextVar("git_opening", default=GitOpening())


class GitLaunch(Executable):
    """One Git child boundary, also usable by the isolated stdlib bootstrap."""

    program: ClassVar[str] = "git"
    chdir_option: ClassVar[str] = "-C"
    directory_options: ClassVar[frozenset[str]] = frozenset(
        {chdir_option, "--git-dir", "--work-tree"}
    )
    value_options: ClassVar[tuple[str, ...]] = (
        *sorted(directory_options),
        "-c",
        "--config-env",
        "--namespace",
        "--list-cmds",
        "--attr-source",
        "--super-prefix",
    )
    optional_options: ClassVar[tuple[str, ...]] = ("--exec-path",)
    switch_options: ClassVar[tuple[str, ...]] = (
        "-v",
        "-h",
        "-p",
        "-P",
        "--version",
        "--help",
        "--no-pager",
        "--paginate",
        "--html-path",
        "--man-path",
        "--info-path",
        "--no-replace-objects",
        "--bare",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--no-optional-locks",
        "--no-lazy-fetch",
        "--no-advice",
    )

    @classmethod
    def options(cls, args: list[str]) -> GitOptions:
        short = "".join(name[1:] + ":" for name in cls.value_options if len(name) == 2)
        short += "".join(name[1:] for name in cls.switch_options if len(name) == 2)
        long = [name[2:] + "=" for name in cls.value_options if name.startswith("--")]
        long.extend(name[2:] for name in cls.switch_options if name.startswith("--"))
        long.extend(name[2:] + "=?" for name in cls.optional_options)
        values, arguments = getopt.getopt(args, short, long)
        return GitOptions(values, arguments)

    @classmethod
    def command(cls, checkout: Path | str, *args: str) -> tuple[str, ...]:
        return cls.program, cls.chdir_option, str(checkout), *args

    @staticmethod
    @cache
    def _local_environment(program: Path, modified_ns: int) -> frozenset[str]:
        """Git declares local selectors; the binary's stamp keys this immutable answer."""
        command = (str(program), "rev-parse", "--local-env-vars")
        with GitLaunch.opening(
            command, stdin=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
            | {"LC_ALL": "C"},
        ) as process:
            stdout, stderr = process.communicate(timeout=10)
            probe = subprocess.CompletedProcess(
                command, process.wait(), stdout.decode(), stderr.decode())
        probe.check_returncode()
        return frozenset(probe.stdout.splitlines())

    @classmethod
    def environment(cls, source: Mapping[str, str]) -> dict[str, str]:
        """The checkout selects the repository; inherit auth and command configuration."""
        match shutil.which(cls.program, path=source.get("PATH", os.defpath)):
            case str(executable):
                program = Path(executable)
            case _:
                raise FileNotFoundError(cls.program)
        local = cls._local_environment(program, program.stat().st_mtime_ns)
        # GIT_CONFIG_* carries command configuration, including auth and fixture
        # identity; GIT_CONFIG instead selects an alternate configuration file.
        return {
            name: value
            for name, value in source.items()
            if name not in local or name.startswith("GIT_CONFIG_")
        } | {"LC_ALL": "C"}

    @staticmethod
    def opening(
        command: tuple[str, ...], *, env: Mapping[str, str],
        stdin: int | BinaryIO | TextIO | None,
        stdout: int | BinaryIO | TextIO | None,
        stderr: int | BinaryIO | TextIO | None,
    ) -> AbstractContextManager[subprocess.Popen[bytes]]:
        return _git_opening.get().opening(
            command, env=env, stdin=stdin, stdout=stdout, stderr=stderr)

    @staticmethod
    def acquiring() -> AbstractContextManager[None]:
        return _git_opening.get().acquiring()

    @staticmethod
    def enter_phase() -> None:
        _git_opening.get().enter_phase()

    @staticmethod
    def in_context(context: Context) -> Context:
        """Carry this constructor capability through an explicitly isolated worker context."""
        context.run(_git_opening.set, _git_opening.get())
        return context

    @classmethod
    def _run(
        cls, checkout: Path | str, *args: str, timeout: float = 60, input: bytes = b""
    ) -> subprocess.CompletedProcess[bytes]:
        command = cls.command(checkout, *args)
        with cls.opening(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=cls.environment(os.environ)) as process:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
            return subprocess.CompletedProcess(command, process.wait(), stdout, stderr)

    @classmethod
    def _pipe(cls, checkout: Path | str, producer: tuple[str, ...], consumer: tuple[str, ...],
              *, timeout: float = 60, input: bytes = b'') -> subprocess.CompletedProcess[bytes]:
        """Stream Git output between children; capture only the final, bounded answer."""
        environment = cls.environment(os.environ)
        producer_command = cls.command(checkout, *producer)
        consumer_command = cls.command(checkout, *consumer)
        started = monotonic()
        # Only revision names and diagnostics are spooled. The potentially enormous patch
        # stream stays in the kernel pipe, and every child is retired on success or failure.
        with TemporaryFile() as source, TemporaryFile() as errors:
            source.write(input)
            source.seek(0)
            with cls.opening(producer_command, stdin=source,
                             stdout=subprocess.PIPE, stderr=errors, env=environment) as first:
                with cls.opening(consumer_command, stdin=first.stdout,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=environment) as last:
                    cast(BinaryIO, first.stdout).close()
                    output, error = last.communicate(
                        timeout=max(0, timeout - (monotonic() - started)))
                result = first.wait(timeout=max(0, timeout - (monotonic() - started)))
                if result:
                    errors.seek(0)
                    return subprocess.CompletedProcess(producer_command, result, b'', errors.read())
                return subprocess.CompletedProcess(consumer_command, last.wait(), output, error)


def built_tree_pin(manifest: Path) -> str:
    """Name the prepared artifact by its scheme and committed file/tree contents.

    Recipe-only edits retain an artifact; a new preparation scheme cannot borrow one
    prepared under the old scheme, even when its content commitment is unchanged.
    """
    built = "".join(
        line
        for line in manifest.read_text().splitlines(keepends=True)
        if not line.startswith(INPUTS_PREFIX)
    )
    return hashlib.sha256((PREPARATION_SCHEME + "\n" + built).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class PackageFile:
    source: str
    target: str

    @classmethod
    def of(cls, record: Mapping[str, str]) -> PackageFile:
        return cls(**record)


@dataclass(frozen=True)
class McpClientSource:
    root: str
    install: tuple[str, ...]
    files: tuple[str, ...]
    trees: tuple[str, ...]

    @classmethod
    def of(cls, record: Mapping[str, object]) -> McpClientSource:
        # JSON enters the declared constructor here; post-init normalizes its arrays.
        return cls(**record)  # type: ignore[arg-type]

    def __post_init__(self) -> None:
        object.__setattr__(self, "install", tuple(self.install))
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "trees", tuple(self.trees))


@dataclass(frozen=True)
class NativeRepositoryInputs:
    package_files: tuple[PackageFile, ...]
    mcp_client: McpClientSource
    compaction_instruction: str

    @classmethod
    def of(cls, root: Path) -> NativeRepositoryInputs:
        return cls(**json.loads((root / "stack/native/repository-inputs.json").read_text()))

    def __post_init__(self) -> None:
        records = cast(tuple[Mapping[str, str], ...], self.package_files)
        object.__setattr__(self, "package_files", tuple(PackageFile.of(item) for item in records))
        object.__setattr__(
            self, "mcp_client", McpClientSource.of(cast(Mapping[str, object], self.mcp_client))
        )

    def pathspecs(self) -> tuple[str, ...]:
        return (
            *(item.source for item in self.package_files),
            *(
                f"{self.mcp_client.root}/{name}"
                for name in (
                    *self.mcp_client.install,
                    *self.mcp_client.files,
                    *self.mcp_client.trees,
                )
            ),
            self.compaction_instruction,
        )


@dataclass(frozen=True)
class NativeBuildInputs(GitLaunch):
    root: Path

    def paths(self) -> tuple[Path, ...]:
        """The files the recipe in stack/native reads: that directory and the
        repository inputs it declares.
        """
        tracked = self._run(
            self.root,
            "ls-files",
            "-z",
            "--cached",
            "--",
            "stack/native",
            *NativeRepositoryInputs.of(self.root).pathspecs(),
        )
        tracked.check_returncode()
        paths = tracked.stdout.decode()
        return tuple(sorted(self.root / path for path in paths.split("\0") if path))

    def digest(self, manifest: Path) -> str:
        digest = hashlib.sha256()
        for path in self.paths():
            digest.update(str(path.relative_to(self.root)).encode() + b"\0")
            digest.update((path.stat().st_mode & 0o111).to_bytes(2, "big"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        for line in manifest.read_text().splitlines():
            if line.startswith("# agent-comms-native-stock-v1 "):
                digest.update(line.encode())
        return digest.hexdigest()

    def require_pinned(self, manifest: Path) -> None:
        if INPUTS_PREFIX + self.digest(manifest) not in manifest.read_text().splitlines():
            raise ValueError("Native build inputs changed; rebuild with --update-pins")

    def preparation_digest(
        self, manifest: Path, node: str, npm: str, dependency_package: str = ""
    ) -> str:
        """Hash the recipe, pin, toolchain and supplied MCP input, never our output."""
        dependency_digest = ""
        if dependency_package:
            dependency_digest = subprocess.check_output(
                [
                    str(self.root / "stack/native/native-source"),
                    sys.executable,
                    "digest",
                    dependency_package,
                ],
                text=True,
            ).strip()
        digest = hashlib.sha256()
        for value in (
            self.digest(manifest),
            built_tree_pin(manifest),
            manifest.read_text(),
            node,
            npm,
            sys.version,
            sys.platform,
            platform.machine(),
            dependency_digest,
        ):
            digest.update(value.encode() + b"\0")
        return digest.hexdigest()


if __name__ == "__main__":
    inputs = NativeBuildInputs(Path(sys.argv[1]))
    manifest = inputs.root / "stack/pi-native.sha256"
    print(built_tree_pin(manifest), inputs.preparation_digest(manifest, *sys.argv[2:]))
