"""Typed launch facts, decoded once from the explicitly selected process record."""
from __future__ import annotations

import json
import hashlib
import os
import sys
from contextlib import contextmanager
from tempfile import TemporaryDirectory
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Annotated, ClassVar

from pydantic import JsonValue

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, JsonShapeFamily, JsonShapeMember, PathText
from .message_reference import MessageReference


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
        return replace(configuration, pythonpath=NoImportPath(), virtual_env=str(self.prefix)).for_interpreter(self.interpreter)

    def environment(self, source: Mapping[str, str], *, configuration: RestartEnvironment) -> dict[str, str]:
        return self.project(configuration).apply_runtime(source)

    def require_available(self) -> None:
        if not self.interpreter.is_file() or not os.access(self.interpreter, os.X_OK):
            raise ValueError("Target interpreter is unavailable")


class RestoredRuntime(LaunchRuntime):
    def environment(self, source: Mapping[str, str], *, configuration: RestartEnvironment) -> dict[str, str]:
        return dict(source)


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


@dataclass(frozen=True)
class EnvironmentRecord:
    """Declarations of external-program environment fields, not Core launch facts."""

    @classmethod
    def variable(cls, name: str) -> str:
        return FieldCodec.record_key(cls, name)

    @classmethod
    def strip[Environment: MutableMapping[str, str]](
        cls, environment: Environment, *names: str,
    ) -> Environment:
        for declaration in fields(cls):
            if not names or declaration.name in names:
                environment.pop(FieldCodec.wire_key(declaration), None)
        return environment

    def encode(self) -> dict[str, str]:
        encoded = FieldCodec.encode(self)
        return {FieldCodec.wire_key(declaration): encoded[FieldCodec.wire_key(declaration)]
                for declaration in fields(self) if "wire_name" in declaration.metadata
                and FieldCodec.wire_key(declaration) in encoded}


@dataclass(frozen=True)
class RestartEnvironment(EnvironmentRecord):
    """Retained external directories and import settings selected by the launch."""

    home: str | None = field(default=None, metadata={"wire_omit_default": True, "wire_name": "HOME"})
    path: str | None = field(default=None, metadata={"wire_omit_default": True, "wire_name": "PATH", "runtime": True})
    pythonpath: ImportPath = field(default_factory=NoImportPath, metadata={"wire_omit_default": True})
    virtual_env: str | None = field(default=None, metadata={"wire_omit_default": True, "wire_name": "VIRTUAL_ENV", "runtime": True})
    config: str | None = field(default=None, metadata={"wire_omit_default": True, "wire_name": "XDG_CONFIG_HOME"})
    native_config: Annotated[Path, PathText] = field(kw_only=True)
    agent_directory: Annotated[Path, PathText] = field(kw_only=True, metadata={"wire_name": "PI_CODING_AGENT_DIR", "native": True})

    def __post_init__(self) -> None:
        object.__setattr__(self, "native_config", self.native_config.expanduser().resolve())
        object.__setattr__(self, "agent_directory", self.agent_directory.expanduser().resolve())

    def encode(self) -> dict[str, str]:
        encoded = super().encode()
        self.pythonpath.apply(encoded)
        return encoded

    def for_agent(self, directory: Path) -> RestartEnvironment:
        return replace(self, agent_directory=directory)

    def without_pythonpath(self) -> RestartEnvironment:
        return replace(self, pythonpath=NoImportPath())

    def for_interpreter(self, interpreter: Path) -> RestartEnvironment:
        directory = str(interpreter.parent)
        inherited = os.defpath if self.path is None else self.path
        path = os.pathsep.join((directory, *(part for part in inherited.split(os.pathsep)
                                             if part != directory)))
        return replace(self, path=path)

    def encode_native(self) -> dict[str, str]:
        encoded = self.encode()
        return {FieldCodec.wire_key(declaration): encoded[FieldCodec.wire_key(declaration)]
                for declaration in fields(self) if declaration.metadata.get("native")}

    def auth_revision(self) -> tuple[int, int]:
        info = (self.native_config / "auth.json").stat()
        return info.st_mtime_ns, info.st_size

    def apply_runtime(self, source: Mapping[str, str]) -> dict[str, str]:
        result = dict(source)
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
class ThreadLaunch:
    name: str
    worktree: str
    tags: frozenset[str]
    parent: str | None
    task: str | None
    managed: bool
    startup_input: str | None

    @classmethod
    def current(cls) -> ThreadLaunch:
        return LaunchRecord.current().require_thread()

    @classmethod
    def called_by(cls, name: str, canonicalize: Callable[[str], str]) -> bool:
        return canonicalize(cls.current().name) == name

    @classmethod
    def take_startup_input(cls) -> str | None:
        record = LaunchRecord.current()
        thread = record.require_thread()
        LaunchRecord.install(replace(record, thread=replace(thread, startup_input=None)))
        return thread.startup_input


@dataclass(frozen=True)
class LaunchRecord:
    """The sole source of Core launch facts for a process and its selected children."""

    root: Annotated[Path, PathText]
    runtime: LaunchRuntime
    configuration: RestartEnvironment
    thread: ThreadLaunch | None
    implicit_root: bool
    agent_bin: str
    agent_args: tuple[str, ...]
    agent_models: tuple[str, ...]
    debug_log: Annotated[Path, PathText] | None
    native_origin: str | None
    login_provider: str | None
    project_socket: Annotated[Path, PathText] | None
    project_request: dict[str, JsonValue] | None
    annotation_grant: MessageReference | None

    argument: ClassVar[str] = "--comms-launch"
    _current: ClassVar[LaunchRecord | None] = None

    def __post_init__(self) -> None:
        if not self.root.is_absolute():
            raise ValueError("A launch record requires an absolute root")
        if not self.agent_bin.strip():
            raise ValueError("A launch record requires its native binary")

    @classmethod
    def current(cls) -> LaunchRecord:
        if cls._current is None:
            raise LaunchRecordUnavailable("No launch record was supplied at startup")
        return cls._current

    @classmethod
    def install(cls, record: LaunchRecord) -> None:
        cls._current = record

    @classmethod
    def read(cls, path: Path) -> LaunchRecord:
        return FieldCodec.decode(cls, json.loads(path.read_text()))

    @classmethod
    def argument_path(cls, arguments) -> Path:
        try:
            index = arguments.index(cls.argument)
            return Path(arguments[index + 1])
        except (ValueError, IndexError) as error:
            raise LaunchRecordUnavailable("An explicit --comms-launch FILE is required") from error

    @classmethod
    def startup(cls, argv: list[str] | None = None) -> LaunchRecord:
        arguments = sys.argv if argv is None else argv
        record = cls.read(cls.argument_path(arguments))
        index = arguments.index(cls.argument)
        del arguments[index:index + 2]
        cls.install(record)
        return record

    @classmethod
    def for_process(cls, pid: int) -> LaunchRecord:
        arguments = [os.fsdecode(item) for item in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if item]
        return cls.read(cls.argument_path(arguments))

    def write(self, path: Path | None = None) -> tuple[str, str]:
        from .store_files import _atomic_write_text
        payload = json.dumps(FieldCodec.encode(self), separators=(",", ":"))
        if path is None:
            path = self.root / "runtime" / "launch" / f"{hashlib.sha256(payload.encode()).hexdigest()}.json"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_write_text(path, payload)
        return self.argument, str(path)

    @contextmanager
    def arguments(self):
        directory = self.root / "runtime"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with TemporaryDirectory(prefix="launch-", dir=directory) as temporary:
            yield self.write(Path(temporary) / "record.json")

    def route(self):
        from .active_route import CommsRoute, resolve_comms_route
        return CommsRoute.current() if self.implicit_root else resolve_comms_route(self.root)

    def require_thread(self) -> ThreadLaunch:
        if self.thread is None:
            raise CallingThreadUnavailable("This launch has no owner thread")
        return self.thread
