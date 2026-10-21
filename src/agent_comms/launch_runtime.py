"""Selected process runtimes and their declared launch environment."""

from __future__ import annotations

import os
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import (
    FieldCodec,
    JsonShapeFamily,
    JsonShapeMember,
    PathText,
    SubclassUnion,
    TextRepresentation,
    boundary_decoder,
    NativeScope,
    RuntimeScope,
    WireName,
    WireOmitDefault,
)
from agent_comms.launch_root import LaunchRoot
from agent_comms.thread_text import AbsentText, GivenText, NoParent, NoThreadText, TextChoice, ThreadParent, ThreadText

if TYPE_CHECKING:
    from agent_comms.threads import Thread


class CallingThreadUnavailable(RelationViolationError):
    """The process environment does not declare its owner thread."""


class InheritedEnvironment(Mapping[str, str]):
    """A child's environment when its launcher names none: this process's own, read at launch."""

    def __getitem__(self, name: str) -> str:
        return os.environ[name]

    def __iter__(self) -> Iterator[str]:
        return iter(os.environ)

    def __len__(self) -> int:
        return len(os.environ)


INHERITED_ENVIRONMENT = InheritedEnvironment()


@dataclass(frozen=True, eq=False)
class LaunchRuntime:
    """One selected installation for a worker and its native executable.

    The interpreter is the runtime's identity; a member decides only what its
    launched children inherit.
    """

    interpreter: Annotated[Path, PathText]

    @classmethod
    def running(cls) -> LaunchRuntime:
        """Keep ordinary launches on the calling process's installation."""
        return RunningRuntime(Path(sys.executable))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, LaunchRuntime) and self.interpreter == other.interpreter

    def __hash__(self) -> int:
        return hash(self.interpreter)

    def child_environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        """A selected installation's child gets exactly the environment its launcher built."""
        return dict(environment)

    @staticmethod
    def current_pointer() -> Path:
        return Path.home() / ".local/share/agent-comms/current"

    @classmethod
    def installed(cls) -> LaunchRuntime:
        return cls(cls.current_pointer().resolve(strict=True) / "bin/python")

    @property
    def prefix(self) -> Path:
        return self.interpreter.parent.parent.resolve()

    @property
    def native_entrypoint(self) -> str:
        return str(self.interpreter.with_name("pi-comms-native"))

    def environment(self, source: Mapping[str, str]) -> dict[str, str]:
        return LaunchRoot.environment(self._environment(source))

    def _environment(self, source: Mapping[str, str]) -> dict[str, str]:
        configuration = replace(
            RestartEnvironment.inherit(source)
            .for_interpreter(self.interpreter)
            .without_pythonpath(),
            virtual_env=SetVariable(str(self.prefix)),
        )
        return configuration.apply_runtime(source)

    def require_available(self) -> None:
        if not self.interpreter.is_file() or not os.access(self.interpreter, os.X_OK):
            raise ValueError("Target interpreter is unavailable")


class RunningRuntime(LaunchRuntime):
    """This process's own interpreter: its child imports the source this process imported."""

    def child_environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        return RunningSource.current().environment(environment)


@dataclass(frozen=True)
class RunningSource(ABC):
    """Where this process imported agent_comms from.

    An editable install's .pth names one checkout; a parent running another
    checkout's source must not hand its children the .pth's copy.
    """

    root: Path

    @staticmethod
    def current() -> RunningSource:
        root = Path(__file__).resolve().parents[1]
        installed = root.is_relative_to(Path(sys.prefix).resolve())
        return (InstalledSource if installed else CheckoutSource)(root)

    @abstractmethod
    def environment(self, environment: Mapping[str, str]) -> dict[str, str]: ...


class InstalledSource(RunningSource):
    """The interpreter's own site-packages: every child of it already imports this copy."""

    def environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        return dict(environment)


class CheckoutSource(RunningSource):
    """A source checkout: its root goes first on the child's import path."""

    def environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        inherited = RestartEnvironment.inherit(environment).with_import_root(self.root)
        return inherited.apply_runtime(environment)


class RestoredRuntime(LaunchRuntime):
    """Original-process recovery preserves its original import environment too."""

    @classmethod
    def running(cls) -> LaunchRuntime:
        return cls(Path(sys.executable))

    def _environment(self, source: Mapping[str, str]) -> dict[str, str]:
        return dict(source)


class ImportPath(DeclaredFamily, JsonShapeFamily, affix="ImportPath"):
    """An inherited Python search path or its explicit removal."""

    @abstractmethod
    def first(self, root: Path) -> ImportPath:
        """This search path with root searched before it."""


@dataclass(frozen=True)
class NoImportPath(ImportPath, JsonShapeMember):
    value: None = None

    def first(self, root: Path) -> ImportPath:
        return SpecifiedImportPath(str(root))


@dataclass(frozen=True)
class SpecifiedImportPath(ImportPath, JsonShapeMember):
    value: str

    def first(self, root: Path) -> ImportPath:
        # Each generation re-applies its root: keep exactly one copy, searched first.
        entries = (entry for entry in self.value.split(os.pathsep) if entry != str(root))
        return SpecifiedImportPath(os.pathsep.join((str(root), *entries)))


class EnvironmentValue(SubclassUnion, TextChoice, affix="Variable"):
    """One inherited environment variable: its text, or unset."""


@dataclass(frozen=True, slots=True)
class SetVariable(GivenText, EnvironmentValue):
    text: str


@dataclass(frozen=True, slots=True)
class UnsetVariable(AbsentText, EnvironmentValue):
    pass


@dataclass(frozen=True)
class EnvironmentRecord:
    """A record whose fields are the environment variables their wire names spell."""

    @classmethod
    def variable(cls, name: str) -> str:
        return FieldCodec.record_key(cls, name)

    @classmethod
    def inherited(cls, environment: Mapping[str, str]) -> dict[str, str]:
        names = (plan.key for plan in FieldCodec.field_plans(cls))
        return {name: environment[name] for name in names if name in environment}

    @classmethod
    def strip[Environment: MutableMapping[str, str]](
        cls, environment: Environment, *names: str
    ) -> Environment:
        """Remove the named fields' variables, or every variable this record declares."""
        for plan in FieldCodec.field_plans(cls):
            if not names or plan.name in names:
                environment.pop(plan.key, None)
        return environment

    def encode(self) -> dict[str, str]:
        encoded: dict[str, str] = FieldCodec.encode(self)
        return {
            plan.key: encoded[plan.key]
            for plan in FieldCodec.field_plans(type(self))
            if plan.key in encoded
        }


@dataclass(frozen=True)
class RestartEnvironment(EnvironmentRecord):
    """Declared inheritance policy for the credential-free resident watcher.

    Launch variable spellings belong to this environment declaration. Owner credentials
    stay in /proc and are read only from the exact selected process at execution.
    """

    home: Annotated[
        EnvironmentValue, WireOmitDefault(), WireName("HOME")
    ] = UnsetVariable()
    path: Annotated[
        EnvironmentValue,
        WireOmitDefault(),
        WireName("PATH"),
        RuntimeScope(),
    ] = UnsetVariable()
    pythonpath: Annotated[
        ImportPath,
        WireOmitDefault(),
        WireName("PYTHONPATH"),
        RuntimeScope(),
    ] = field(default_factory=NoImportPath)
    virtual_env: Annotated[
        EnvironmentValue,
        WireOmitDefault(),
        WireName("VIRTUAL_ENV"),
        RuntimeScope(),
    ] = UnsetVariable()
    config: Annotated[
        EnvironmentValue, WireOmitDefault(), WireName("XDG_CONFIG_HOME")
    ] = UnsetVariable()
    default_agent_directory: ClassVar[Path] = Path("~/.pi/agent")
    native_config: Annotated[
        Path,
        PathText,
        WireName("AGENT_COMMS_NATIVE_CONFIG_DIR"),
        NativeScope(),
    ] = default_agent_directory
    agent_directory: Annotated[
        Path, PathText, WireName("PI_CODING_AGENT_DIR"), NativeScope()
    ] = default_agent_directory

    def __post_init__(self) -> None:
        object.__setattr__(self, "native_config", self.absolute(self.native_config))
        object.__setattr__(self, "agent_directory", self.absolute(self.agent_directory))

    def absolute(self, directory: Path) -> Path:
        if directory.is_relative_to("~"):
            directory = Path(self.home.otherwise(str(Path.home()))) / directory.relative_to("~")
        return directory.expanduser().resolve()

    @classmethod
    def inherit(cls, environment: Mapping[str, str]):
        values = cls.inherited(environment)
        native_name, agent_name = cls.variable("native_config"), cls.variable("agent_directory")
        values[native_name] = (
            values.get(native_name) or values.get(agent_name) or str(cls.default_agent_directory)
        )
        return FieldCodec.decode(cls, values)

    def for_agent(self, directory: Path) -> RestartEnvironment:
        """Change only the acquired writable resource, retaining original discovery."""
        return replace(self, agent_directory=directory)

    def without_pythonpath(self) -> RestartEnvironment:
        return replace(self, pythonpath=NoImportPath())

    def with_import_root(self, root: Path) -> RestartEnvironment:
        return replace(self, pythonpath=self.pythonpath.first(root))

    def for_interpreter(self, interpreter: Path) -> RestartEnvironment:
        """Put this process's runtime first, exactly once; preserve other search paths."""
        directory = str(interpreter.parent)
        inherited = self.path.otherwise(os.defpath)
        path = os.pathsep.join(
            (directory, *(entry for entry in inherited.split(os.pathsep) if entry != directory))
        )
        return replace(self, path=SetVariable(path))

    def encode_native(self) -> dict[str, str]:
        encoded = self.encode()
        native = (
            plan.key
            for plan in FieldCodec.field_plans(type(self))
            if plan.setting(NativeScope, False)
        )
        return {key: encoded[key] for key in native}

    def auth_revision(self) -> tuple[int, int]:
        """Observe this owner's credentials, never an ambient or fork directory.

        No credentials file yet is setup state, observed as a size no file has.
        """
        try:
            info = (self.native_config / "auth.json").stat()
        except FileNotFoundError:
            return 0, -1
        return info.st_mtime_ns, info.st_size

    def apply_runtime(self, source: Mapping[str, str]) -> dict[str, str]:
        """Preserve source credentials/settings; replace the declared runtime fields."""
        result = dict(source)
        encoded = self.encode()
        for plan in FieldCodec.field_plans(type(self)):
            if plan.setting(RuntimeScope, False):
                name = plan.key
                result.pop(name, None)
                if name in encoded:
                    result[name] = encoded[name]
        return result


class TagListText(TextRepresentation[frozenset[str]]):
    @classmethod
    def encode(cls, value: object) -> str:
        if not isinstance(value, frozenset):
            raise TypeError("Expected a set of tags")
        return ",".join(sorted(value))

    @classmethod
    def from_text(cls, value: str) -> frozenset[str]:
        return frozenset(tag.strip() for tag in value.split(",") if tag.strip())


class FlagText(TextRepresentation[bool]):
    @classmethod
    def encode(cls, value: object) -> str:
        if not isinstance(value, bool):
            raise TypeError("Expected a flag")
        return str(int(value))

    @classmethod
    def from_text(cls, value: str) -> bool:
        return bool(int(value))


@dataclass(frozen=True)
class ThreadLaunch(EnvironmentRecord):
    """The environment that carries one thread's launch into its processes.

    Launch variable spellings for a thread belong to these fields; writers encode
    the record and readers decode it.
    """

    name: Annotated[str, WireName("AGENT_COMMS_THREAD")]
    worktree: Annotated[str, WireName("PI_WORKTREE")]
    tags: Annotated[
        frozenset[str],
        TagListText,
        WireOmitDefault(),
        WireName("AGENT_COMMS_TAGS"),
    ] = frozenset()
    parent: Annotated[
        ThreadParent, WireOmitDefault(), WireName("PI_PARENT_ID")
    ] = NoParent()
    task: Annotated[
        ThreadText, WireOmitDefault(), WireName("PI_TASK")
    ] = NoThreadText()
    managed: Annotated[
        bool,
        FlagText,
        WireOmitDefault(),
        WireName("AGENT_COMMS_MANAGED"),
    ] = False
    startup_input: Annotated[
        EnvironmentValue,
        WireOmitDefault(),
        WireName("AGENT_COMMS_STARTUP_INPUT_KEY"),
    ] = UnsetVariable()

    @classmethod
    @boundary_decoder
    def from_thread(cls, thread: Thread) -> ThreadLaunch:
        return cls(
            thread.name,
            thread.worktree,
            frozenset(thread.tags),
            thread.parent,
            thread.task,
        )

    @classmethod
    def current(cls, environment: Mapping[str, str] = os.environ) -> ThreadLaunch:
        """Decode this process's launch; an unset worktree is the working directory."""
        values = cls.inherited(environment)
        if not values.get(cls.variable("name")):
            raise CallingThreadUnavailable(f"Calling thread requires {cls.variable('name')}.")
        values.setdefault(cls.variable("worktree"), os.getcwd())
        return FieldCodec.decode(cls, values)

    @classmethod
    def called_by(
        cls,
        name: str,
        canonicalize: Callable[[str], str],
        *,
        environment: Mapping[str, str] = os.environ,
    ) -> bool:
        try:
            caller = cls.current(environment).name
        except CallingThreadUnavailable:
            return False
        return canonicalize(caller) == name

    @classmethod
    def take_startup_input(cls, environment: MutableMapping[str, str] = os.environ) -> str | None:
        """This launch's startup input, removed so no reread or child process sees it."""
        startup_input = cls.current(environment).startup_input
        cls.strip(environment, "startup_input")
        # The worker still declares no startup input as None.
        return startup_input.otherwise(None)

    def apply(self, environment: dict[str, str]) -> dict[str, str]:
        """Replace any inherited launch with this one."""
        return self.strip(environment) | self.encode()
