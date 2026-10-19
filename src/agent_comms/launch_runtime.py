"""Typed launch facts, decoded once from the explicitly selected process record."""
from __future__ import annotations

import json
import asyncio
from abc import abstractmethod
import os
import sys
from contextlib import asynccontextmanager, contextmanager
from tempfile import TemporaryFile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Annotated, ClassVar, Self, get_args, get_type_hints

from pydantic import JsonValue

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, JsonShapeFamily, JsonShapeMember, PathText


class CallingThreadUnavailable(RelationViolationError):
    """The selected launch does not declare an owner thread."""


class LaunchRecordUnavailable(RelationViolationError):
    """No explicit launch record was supplied to this process."""


@dataclass(frozen=True)
class LaunchRuntime:
    interpreter: Annotated[Path, PathText]

    @classmethod
    def running(cls) -> LaunchRuntime:
        return LaunchRecord.current().runtime

    @property
    def prefix(self) -> Path:
        return self.interpreter.parent.parent.resolve()

    @property
    def native_entrypoint(self) -> str:
        return str(self.interpreter.with_name("pi-comms-native"))

    def project(self, configuration: RestartEnvironment) -> RestartEnvironment:
        return replace(configuration, virtual_env=SpecifiedEnvironmentValue(str(self.prefix))).for_interpreter(self.interpreter)

    def environment(self, source: Mapping[str, str], *, configuration: RestartEnvironment) -> dict[str, str]:
        return self.project(configuration).apply_runtime(source)

    def require_available(self) -> None:
        if not self.interpreter.is_file() or not os.access(self.interpreter, os.X_OK):
            raise ValueError("Target interpreter is unavailable")


class ImportPath(DeclaredFamily, JsonShapeFamily, affix="ImportPath"):
    """An inherited Python search path or its explicit removal."""


@dataclass(frozen=True)
class NoImportPath(ImportPath, JsonShapeMember):
    value: None = None

    def apply(self, environment) -> None:
        environment.pop("PYTHONPATH", None)


@dataclass(frozen=True)
class SpecifiedImportPath(ImportPath, JsonShapeMember):
    value: str

    def apply(self, environment) -> None:
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

    home: EnvironmentValue = field(default_factory=AbsentEnvironmentValue, metadata={"wire_omit_default": True, "wire_name": "HOME"})
    path: EnvironmentValue = field(default_factory=AbsentEnvironmentValue, metadata={"wire_omit_default": True, "wire_name": "PATH", "runtime": True})
    pythonpath: ImportPath = field(default_factory=NoImportPath, metadata={"wire_omit_default": True})
    virtual_env: EnvironmentValue = field(default_factory=AbsentEnvironmentValue, metadata={"wire_omit_default": True, "wire_name": "VIRTUAL_ENV", "runtime": True})
    config: EnvironmentValue = field(default_factory=AbsentEnvironmentValue, metadata={"wire_omit_default": True, "wire_name": "XDG_CONFIG_HOME"})
    native_config: Annotated[Path, CapturedDirectoryText] = field(kw_only=True)
    agent_directory: Annotated[Path, CapturedDirectoryText, NativeField()] = field(kw_only=True, metadata={"wire_name": "PI_CODING_AGENT_DIR"})

    @classmethod
    def capture(cls, source: Mapping[str, str], *, native_config: Path, agent_directory: Path) -> Self:
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
class LaunchChange(DeclaredFamily, affix="LaunchChange"):
    @abstractmethod
    def apply(self, record: LaunchRecord) -> LaunchRecord: ...


class NoLaunchChange(LaunchChange):
    def apply(self, record: LaunchRecord) -> LaunchRecord:
        return record


@dataclass(frozen=True)
class EnvironmentLaunchChange(LaunchChange):
    configuration: RestartEnvironment

    def apply(self, record: LaunchRecord) -> LaunchRecord:
        return replace(record, configuration=self.configuration)


@dataclass(frozen=True)
class CommandLaunchChange(LaunchChange):
    value: str

    def apply(self, record: LaunchRecord) -> LaunchRecord:
        return replace(record, agent_bin=self.value)


@dataclass(frozen=True)
class ArgumentsLaunchChange(LaunchChange):
    value: tuple[str, ...]

    def apply(self, record: LaunchRecord) -> LaunchRecord:
        return replace(record, agent_args=self.value)


@dataclass(frozen=True)
class SourceInterpreter(DeclaredFamily, affix="SourceInterpreter"):
    @abstractmethod
    def require(self, observed: str) -> None: ...


class AnySourceInterpreter(SourceInterpreter):
    def require(self, observed: str) -> None:
        pass


@dataclass(frozen=True)
class ExactSourceInterpreter(SourceInterpreter):
    value: str

    def require(self, observed: str) -> None:
        if observed != self.value:
            raise RelationViolationError("Selected source owner interpreter changed")


@dataclass(frozen=True)
class StartupInput(DeclaredFamily, affix="StartupInput"):
    @asynccontextmanager
    async def running(self, operation: Callable[[str], Awaitable[object]]) -> AsyncIterator[StartupInput]:
        yield self


class NoStartupInput(StartupInput):
    pass


@dataclass(frozen=True)
class QueuedStartupInput(StartupInput):
    key: str

    @asynccontextmanager
    async def running(self, operation: Callable[[str], Awaitable[object]]) -> AsyncIterator[StartupInput]:
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
    def route(self): ...

    @abstractmethod
    def write_scope(self): ...


class ExplicitLaunchRoot(LaunchRoot):
    def write_scope(self):
        from contextlib import nullcontext
        return nullcontext()

    def route(self):
        from .active_route import resolve_comms_route
        return resolve_comms_route(self.path)


class CurrentLaunchRoot(LaunchRoot):
    @classmethod
    def capture(cls) -> CurrentLaunchRoot:
        from .active_route import CommsRoute
        return cls(CommsRoute.current().observe_root())

    @contextmanager
    def write_scope(self):
        from .active_route import guard_default_route_write
        from .route_selection import RouteChanged
        with guard_default_route_write(self.path):
            if self.route().observe_root() != self.path:
                raise RouteChanged("default Comms route changed before write")
            yield

    def route(self):
        from .active_route import CommsRoute
        return CommsRoute.current()


@dataclass(frozen=True)
class NativeOriginPolicy(DeclaredFamily, affix="NativeOriginPolicy"):
    pass


class UnrestrictedNativeOriginPolicy(NativeOriginPolicy):
    pass


@dataclass(frozen=True)
class LoopbackNativeOriginPolicy(NativeOriginPolicy):
    value: str


@dataclass(frozen=True)
class LaunchDebug(DeclaredFamily, affix="LaunchDebug"):
    def configure(self) -> None:
        pass

    def append(self, text: str) -> None:
        pass

    def connection_options(self, observer):
        return {}

    async def serve(self, operation):
        return await operation()


class DisabledLaunchDebug(LaunchDebug):
    pass


@dataclass(frozen=True)
class FileLaunchDebug(LaunchDebug):
    path: Annotated[Path, PathText]

    def configure(self) -> None:
        import logging
        logging.basicConfig(level=logging.DEBUG, filename=str(self.path) + ".log",
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")

    def append(self, text: str) -> None:
        with self.path.open("a") as output:
            output.write(text)

    def connection_options(self, observer):
        return {"observers": [observer]}

    async def serve(self, operation):
        import asyncio
        import traceback
        async def watchdog():
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

    def __iter__(self):
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
    annotation: AnnotationPolicy = field(default_factory=lambda: DisabledAnnotationPolicy(), kw_only=True)

    argument: ClassVar[str] = "--comms-launch"
    _current: ClassVar[LaunchRecord]

    def at_root(self, path: Path) -> Self:
        return replace(self, root_selection=ExplicitLaunchRoot(Path(path).resolve()))

    @classmethod
    def from_record(cls, record: LaunchRecord, **specific):
        encoded = FieldCodec.encode(record)
        values = {key: encoded[key] for _, key in FieldCodec.record_fields(LaunchRecord)
                  if key in encoded}
        annotations = get_type_hints(cls, include_extras=True)
        values.update({FieldCodec.record_key(cls, key): FieldCodec.encode(value, annotations[key])
                       for key, value in specific.items()})
        return FieldCodec.decode_record(cls, values)

    @classmethod
    def current(cls) -> LaunchRecord:
        try:
            return cls._current
        except AttributeError as error:
            raise LaunchRecordUnavailable("No launch record was supplied at startup") from error

    @classmethod
    def install(cls, record: LaunchRecord) -> None:
        LaunchRecord._current = record

    @classmethod
    def read(cls, path: Path) -> LaunchRecord:
        return FieldCodec.decode(LaunchRecord, json.loads(path.read_text()))

    @classmethod
    def argument_path(cls, arguments) -> Path:
        try:
            index = arguments.index(cls.argument)
            return Path(arguments[index + 1])
        except (ValueError, IndexError) as error:
            raise LaunchRecordUnavailable("An explicit --comms-launch FILE is required") from error

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
        arguments = [os.fsdecode(item) for item in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if item]
        path = Path(f"/proc/{pid}/fd") / cls.argument_path(arguments).name
        return cls.read(path)

    @contextmanager
    def arguments(self):
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

    def prepare(self, resources) -> Self:
        return self


class CommandLineLaunchRecord(LaunchRecord):
    @classmethod
    def entrypoint(cls, root: LaunchRoot) -> CommandLineLaunchRecord:
        directory = Path.home() / ".pi" / "agent"
        return cls(root, LaunchRuntime(Path(sys.executable)),
                   RestartEnvironment.capture(os.environ, native_config=directory, agent_directory=directory),
                   "pi", (), ())


@dataclass(frozen=True)
class OwnerLaunchRecord(LaunchRecord):
    thread: ThreadLaunch

    def called_by(self, name: str, canonicalize: Callable[[str], str]) -> bool:
        return canonicalize(self.thread.name) == name

    def require_thread(self) -> ThreadLaunch:
        return self.thread

    def take_startup_input(self) -> StartupInput:
        value = self.thread.startup_input
        LaunchRecord.install(replace(self, thread=replace(self.thread, startup_input=NoStartupInput())))
        return value


class NativeOwnerLaunchRecord(OwnerLaunchRecord):
    pass


@dataclass(frozen=True)
class ProjectLaunchRecord(NativeOwnerLaunchRecord):
    project_socket: Annotated[Path, PathText]
    project_request: dict[str, JsonValue]

    def prepare(self, resources) -> ProjectLaunchRecord:
        from .private_path import PrivateSocketRole
        address = resources.enter_context(PrivateSocketRole.address(self.project_socket))
        return replace(self, project_socket=address)


@dataclass(frozen=True)
class LoginLaunchRecord(LaunchRecord):
    login_provider: str


class AcpLaunchRecord(LaunchRecord):
    pass


class RestartQueueLaunchRecord(LaunchRecord):
    pass


from .working_memory_policy import AnnotationPolicy, DisabledAnnotationPolicy
