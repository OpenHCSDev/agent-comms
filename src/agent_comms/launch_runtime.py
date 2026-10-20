"""Typed launch facts, decoded once from the explicitly selected process record."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import traceback
from abc import abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from tempfile import TemporaryFile
from typing import TYPE_CHECKING, Annotated, ClassVar, Self, get_args, get_type_hints

from pydantic import JsonValue

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, JsonShapeFamily, JsonShapeMember, PathText
from .private_path import PrivateSocketRole
from .working_memory_policy import AnnotationPolicy, DisabledAnnotationPolicy

if TYPE_CHECKING:
    from acp.connection import StreamObserver

    from .active_route import CommsRoute


class CallingThreadUnavailable(RelationViolationError):
    """The selected launch does not declare an owner thread."""


class LaunchRecordUnavailableError(RelationViolationError):
    """No explicit launch record was supplied to this process."""


@dataclass(frozen=True)
class LaunchRuntime:
    interpreter: Annotated[Path, PathText]

    @classmethod
    def running(cls) -> Self:
        return cls(Path(sys.executable))

    @property
    def prefix(self) -> Path:
        return self.interpreter.parent.parent.resolve()

    @property
    def native_entrypoint(self) -> str:
        return str(self.interpreter.with_name("pi-comms-native"))

    def project(self, configuration: RestartEnvironment) -> RestartEnvironment:
        return replace(
            configuration, virtual_env=SpecifiedEnvironmentValue(str(self.prefix))
        ).for_interpreter(self.interpreter)

    def require_available(self) -> None:
        if not self.interpreter.is_file() or not os.access(self.interpreter, os.X_OK):
            raise ValueError("Target interpreter is unavailable")


class RestoredRuntime(LaunchRuntime):
    """Original-process recovery preserves its acquired import environment."""

    def environment(self, source: Mapping[str, str]) -> dict[str, str]:
        return dict(source)


class ImportPath(DeclaredFamily, JsonShapeFamily, affix="ImportPath"):
    """An inherited Python search path or its explicit removal."""

    @abstractmethod
    def apply(self, environment: dict[str, str]) -> None: ...


@dataclass(frozen=True)
class NoImportPath(ImportPath, JsonShapeMember):
    value: None = None

    def apply(self, environment: dict[str, str]) -> None:
        environment.pop("PYTHONPATH", None)


@dataclass(frozen=True)
class SpecifiedImportPath(ImportPath, JsonShapeMember):
    value: str

    def apply(self, environment: dict[str, str]) -> None:
        environment["PYTHONPATH"] = self.value


class EnvironmentValue(DeclaredFamily, JsonShapeFamily, affix="EnvironmentValue"):
    @abstractmethod
    def prepend_search(self, directory: str) -> EnvironmentValue: ...


@dataclass(frozen=True)
class AbsentEnvironmentValue(EnvironmentValue):
    def prepend_search(self, directory: str) -> EnvironmentValue:
        # An unset PATH uses the operating system's defined executable search path.
        return SpecifiedEnvironmentValue(os.defpath).prepend_search(directory)


@dataclass(frozen=True)
class SpecifiedEnvironmentValue(EnvironmentValue, JsonShapeMember):
    value: str

    def prepend_search(self, directory: str) -> EnvironmentValue:
        parts = (part for part in self.value.split(os.pathsep) if part != directory)
        return SpecifiedEnvironmentValue(os.pathsep.join((directory, *parts)))


class CapturedDirectoryText(PathText):
    @staticmethod
    def capture(path: Path) -> Path:
        return path.expanduser().resolve()

    @classmethod
    def from_text(cls, value: str) -> Path:
        return cls.capture(Path(value))


class NativeField:
    """A launch field exported to the native process environment."""


@dataclass(frozen=True)
class RestartEnvironment:
    """Retained external directories and import settings selected by the launch."""

    home: EnvironmentValue = field(
        default_factory=AbsentEnvironmentValue,
        metadata={"wire_omit_default": True, "wire_name": "HOME"},
    )
    path: EnvironmentValue = field(
        default_factory=AbsentEnvironmentValue,
        metadata={"wire_omit_default": True, "wire_name": "PATH", "runtime": True},
    )
    pythonpath: ImportPath = field(
        default_factory=NoImportPath, metadata={"wire_omit_default": True}
    )
    virtual_env: EnvironmentValue = field(
        default_factory=AbsentEnvironmentValue,
        metadata={"wire_omit_default": True, "wire_name": "VIRTUAL_ENV", "runtime": True},
    )
    config: EnvironmentValue = field(
        default_factory=AbsentEnvironmentValue,
        metadata={"wire_omit_default": True, "wire_name": "XDG_CONFIG_HOME"},
    )
    native_config: Annotated[Path, CapturedDirectoryText] = field(kw_only=True)
    agent_directory: Annotated[Path, CapturedDirectoryText, NativeField()] = field(
        kw_only=True, metadata={"wire_name": "PI_CODING_AGENT_DIR"},
    )

    @classmethod
    def capture(
        cls, source: Mapping[str, str], *, native_config: Path, agent_directory: Path,
    ) -> Self:
        values = {FieldCodec.wire_key(declaration): source[FieldCodec.wire_key(declaration)]
                  for declaration in fields(cls) if "wire_name" in declaration.metadata
                  and FieldCodec.wire_key(declaration) in source}
        values[FieldCodec.record_key(cls, "native_config")] = str(native_config)
        values[FieldCodec.record_key(cls, "agent_directory")] = str(agent_directory)
        return FieldCodec.decode(cls, values)

    def encode(self) -> dict[str, str]:
        wire = FieldCodec.encode(self)
        encoded = {FieldCodec.wire_key(item): wire[FieldCodec.wire_key(item)]
                   for item in fields(self) if "wire_name" in item.metadata
                   and FieldCodec.wire_key(item) in wire}
        self.pythonpath.apply(encoded)
        return encoded

    def for_agent(self, directory: Path) -> RestartEnvironment:
        return replace(self, agent_directory=CapturedDirectoryText.capture(directory))

    def for_interpreter(self, interpreter: Path) -> RestartEnvironment:
        directory = str(interpreter.parent)
        return replace(self, path=self.path.prepend_search(directory))

    def encode_native(self) -> dict[str, str]:
        encoded = self.encode()
        annotations = get_type_hints(type(self), include_extras=True)
        return {FieldCodec.wire_key(declaration): encoded[FieldCodec.wire_key(declaration)]
                for declaration in fields(self) if any(isinstance(marker, NativeField)
                    for marker in get_args(annotations[declaration.name]))}

    def auth_revision(self) -> tuple[int, int]:
        info = (self.native_config / "auth.json").stat()
        return info.st_mtime_ns, info.st_size

    def apply_runtime(self, source: Mapping[str, str]) -> dict[str, str]:
        result = {name: value for name, value in source.items()
                  if not name.startswith("AGENT_COMMS_")}
        encoded = self.encode()
        for declaration in fields(self):
            if declaration.metadata.get("runtime"):
                name = FieldCodec.wire_key(declaration)
                result.pop(name, None)
                if name in encoded:
                    result[name] = encoded[name]
        self.pythonpath.apply(result)
        return result


@dataclass(frozen=True)
class StartupInput(DeclaredFamily, affix="StartupInput"):
    @asynccontextmanager
    async def running(
        self, operation: Callable[[str], Awaitable[object]],
    ) -> AsyncIterator[StartupInput]:
        yield self


class NoStartupInput(StartupInput):
    pass


@dataclass(frozen=True)
class QueuedStartupInput(StartupInput):
    key: str

    @asynccontextmanager
    async def running(
        self, operation: Callable[[str], Awaitable[object]],
    ) -> AsyncIterator[StartupInput]:
        task = asyncio.ensure_future(operation(self.key))
        try:
            yield self
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@dataclass(frozen=True)
class ThreadLaunch:
    name: str
    startup_input: StartupInput = field(default_factory=NoStartupInput)

@dataclass(frozen=True)
class LaunchRoot(DeclaredFamily, affix="LaunchRoot"):
    path: Annotated[Path, PathText]

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("A launch requires an absolute root")

    @abstractmethod
    def route(self) -> CommsRoute: ...

    @abstractmethod
    def write_scope(self) -> AbstractContextManager[None]: ...

@dataclass(frozen=True)
class NativeOriginPolicy(DeclaredFamily, affix="NativeOriginPolicy"):
    def native_values(self) -> dict[str, str]:
        return {}


class UnrestrictedNativeOriginPolicy(NativeOriginPolicy):
    pass


@dataclass(frozen=True)
class LoopbackNativeOriginPolicy(NativeOriginPolicy):
    value: str

    def native_values(self) -> dict[str, str]:
        return {"AGENT_COMMS_NATIVE_ORIGIN": self.value}


@dataclass(frozen=True)
class LaunchDebug(DeclaredFamily, affix="LaunchDebug"):
    def configure(self) -> None:
        pass

    def append(self, text: str) -> None:
        pass

    def connection_options(self, observer: StreamObserver) -> dict[str, list[StreamObserver]]:
        return {}

    async def serve[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        return await operation()


class DisabledLaunchDebug(LaunchDebug):
    pass


@dataclass(frozen=True)
class FileLaunchDebug(LaunchDebug):
    path: Annotated[Path, PathText]

    def configure(self) -> None:
        logging.basicConfig(level=logging.DEBUG, filename=str(self.path) + ".log",
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")

    def append(self, text: str) -> None:
        with self.path.open("a") as output:
            output.write(text)

    def connection_options(self, observer: StreamObserver) -> dict[str, list[StreamObserver]]:
        return {"observers": [observer]}

    async def serve[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        async def watchdog() -> None:
            while True:
                await asyncio.sleep(5)
                rows = [f"=== watchdog: {len(asyncio.all_tasks())} tasks ===\n"]
                for task in asyncio.all_tasks():
                    rows.append(f"{task!r}\n")
                    for frame in task.get_stack():
                        rows.extend(traceback.format_stack(frame))
                self.append("".join(rows))
        task = asyncio.create_task(watchdog())
        try:
            return await operation()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@dataclass(frozen=True)
class LaunchArguments:
    argv: tuple[str, str]
    pass_fds: tuple[int, ...]

    def __iter__(self) -> Iterator[str]:
        return iter(self.argv)


@dataclass(frozen=True)
class LaunchRecord(DeclaredFamily, affix="LaunchRecord"):
    """Every launch has these facts; capabilities live on their required variants."""

    root_selection: LaunchRoot
    runtime: LaunchRuntime
    configuration: RestartEnvironment
    agent_bin: str
    agent_args: tuple[str, ...]
    agent_models: tuple[str, ...]
    debug: LaunchDebug = field(default_factory=DisabledLaunchDebug, kw_only=True)
    origin: NativeOriginPolicy = field(default_factory=UnrestrictedNativeOriginPolicy, kw_only=True)
    annotation: AnnotationPolicy = field(
        default_factory=lambda: DisabledAnnotationPolicy(), kw_only=True
    )

    argument: ClassVar[str] = "--comms-launch"
    _current: ClassVar[LaunchRecord]

    @classmethod
    def from_record(cls, record: LaunchRecord, **specific: object) -> Self:
        encoded = FieldCodec.encode(record)
        values = {key: encoded[key] for _, key in FieldCodec.record_fields(LaunchRecord)
                  if key in encoded}
        annotations = get_type_hints(cls, include_extras=True)
        values.update({FieldCodec.record_key(cls, key): FieldCodec.encode(value, annotations[key])
                       for key, value in specific.items()})
        return FieldCodec.decode_record(cls, values)

    def with_command(
        self, agent_bin: str | None, agent_args: Sequence[str] | None,
    ) -> Self:
        return replace(
            self,
            agent_bin=self.agent_bin if agent_bin is None else agent_bin,
            agent_args=self.agent_args if agent_args is None else tuple(agent_args),
        )

    def environment(self, source: Mapping[str, str]) -> dict[str, str]:
        return self.runtime.project(self.configuration).apply_runtime(source)

    @classmethod
    def current(cls) -> LaunchRecord:
        try:
            return cls._current
        except AttributeError as error:
            raise LaunchRecordUnavailableError(
                "No launch record was supplied at startup"
            ) from error

    @classmethod
    def install(cls, record: LaunchRecord) -> None:
        LaunchRecord._current = record

    @classmethod
    def read(cls, path: Path) -> LaunchRecord:
        return FieldCodec.decode(LaunchRecord, json.loads(path.read_text()))

    @classmethod
    def argument_path(cls, arguments: Sequence[str]) -> Path:
        try:
            index = arguments.index(cls.argument)
            return Path(arguments[index + 1])
        except (ValueError, IndexError) as error:
            raise LaunchRecordUnavailableError(
                "An explicit --comms-launch FILE is required"
            ) from error

    @classmethod
    def startup(cls, argv: list[str]) -> LaunchRecord:
        arguments = argv
        record = cls.read(cls.argument_path(arguments))
        index = arguments.index(cls.argument)
        del arguments[index:index + 2]
        cls.install(record)
        return record

    @classmethod
    def client_startup(cls, arguments: list[str], root: LaunchRoot) -> LaunchRecord:
        """Install a frontend's launch before any route consumer is constructed."""
        if cls.argument in arguments:
            return cls.startup(arguments)
        record = CommandLineLaunchRecord.entrypoint(root)
        cls.install(record)
        return record

    @classmethod
    def for_process(cls, pid: int) -> LaunchRecord:
        arguments = [
            os.fsdecode(item)
            for item in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if item
        ]
        path = Path(f"/proc/{pid}/fd") / cls.argument_path(arguments).name
        return cls.read(path)

    @contextmanager
    def arguments(self) -> Iterator[LaunchArguments]:
        """One writer; the inherited descriptor keeps the unlinked file until child exit."""
        directory = self.root_selection.path / "runtime"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with TemporaryFile(mode="w+b", dir=directory) as stream:
            stream.write(json.dumps(FieldCodec.encode(self), separators=(",", ":")).encode())
            stream.flush()
            descriptor = stream.fileno()
            yield LaunchArguments((self.argument, f"/dev/fd/{descriptor}"), (descriptor,))

    def called_by(self, name: str, canonicalize: Callable[[str], str]) -> bool:
        return False

    def require_thread(self) -> ThreadLaunch:
        raise CallingThreadUnavailable("This launch has no owner thread")

    def take_startup_input(self) -> StartupInput:
        raise CallingThreadUnavailable("This launch has no startup input")

    def prepare(self, resources: ExitStack) -> Self:
        return self

    def native_values(self, cwd: Path) -> dict[str, str]:
        return {"PI_WORKTREE": str(cwd)} | self.origin.native_values()


class CommandLineLaunchRecord(LaunchRecord):
    @classmethod
    def entrypoint(cls, root: LaunchRoot) -> CommandLineLaunchRecord:
        directory = Path.home() / ".pi" / "agent"
        return cls(root, LaunchRuntime(Path(sys.executable)),
                   RestartEnvironment.capture(
                       os.environ, native_config=directory, agent_directory=directory),
                   "pi", (), ())


@dataclass(frozen=True)
class OwnerLaunchRecord(LaunchRecord):
    thread: ThreadLaunch

    def called_by(self, name: str, canonicalize: Callable[[str], str]) -> bool:
        return canonicalize(self.thread.name) == name

    def require_thread(self) -> ThreadLaunch:
        return self.thread

    def native_values(self, cwd: Path) -> dict[str, str]:
        return super().native_values(cwd) | {
            "AGENT_COMMS_THREAD": self.thread.name, "AGENT_COMMS_MANAGED": "1",
        }

    def take_startup_input(self) -> StartupInput:
        value = self.thread.startup_input
        LaunchRecord.install(replace(
            self, thread=replace(self.thread, startup_input=NoStartupInput())))
        return value


class NativeOwnerLaunchRecord(OwnerLaunchRecord):
    pass


@dataclass(frozen=True)
class ProjectLaunchRecord(NativeOwnerLaunchRecord):
    project_socket: Annotated[Path, PathText]
    project_request: dict[str, JsonValue]

    def native_values(self, cwd: Path) -> dict[str, str]:
        return super().native_values(cwd) | {
            "AGENT_COMMS_PROJECT_SOCKET": str(self.project_socket),
            "AGENT_COMMS_PROJECT_REQUEST": json.dumps(self.project_request),
        }

    def prepare(self, resources: ExitStack) -> Self:
        address = resources.enter_context(PrivateSocketRole.address(self.project_socket))
        return replace(self, project_socket=address)


@dataclass(frozen=True)
class LoginLaunchRecord(LaunchRecord):
    login_provider: str

    def native_values(self, cwd: Path) -> dict[str, str]:
        return super().native_values(cwd) | {"AGENT_COMMS_LOGIN_PROVIDER": self.login_provider}


class AcpLaunchRecord(LaunchRecord):
    pass


class RestartQueueLaunchRecord(LaunchRecord):
    pass
