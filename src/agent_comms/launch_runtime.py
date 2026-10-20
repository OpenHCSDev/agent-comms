"""Selected process runtimes and their declared launch environment."""

from __future__ import annotations

import os
import sys
from abc import abstractmethod
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from pydantic import BaseModel


from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import (
    FieldCodec,
    GitCommitText,
    JsonShapeFamily,
    JsonShapeMember,
    NullMember,
    PathText,
    SubclassUnion,
    TextRepresentation,
    boundary_decoder,
    NativeScope,
    RuntimeScope,
    WireName,
    WireOmitDefault,
)
from .launch_root import LaunchRoot

if TYPE_CHECKING:
    from .threads import Thread


class CallingThreadUnavailable(RelationViolationError):
    """The process environment does not declare its owner thread."""


@dataclass(frozen=True)
class LaunchRuntime:
    """One selected installation for a worker and its native executable."""

    interpreter: Annotated[Path, PathText]

    @classmethod
    def running(cls) -> LaunchRuntime:
        """Keep ordinary launches on the calling process's installation."""
        return cls(Path(sys.executable))

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
            virtual_env=str(self.prefix),
        )
        return configuration.apply_runtime(source)

    def require_available(self) -> None:
        if not self.interpreter.is_file() or not os.access(self.interpreter, os.X_OK):
            raise ValueError("Target interpreter is unavailable")


class ProofBuildMismatchError(ValueError):
    """A retained proof does not identify the currently installed pair."""


class InstalledBuildManifest(BaseModel):
    """Commit fields in the installer's build.json; unrelated build details remain there."""

    core_commit: str
    toad_commit: str


class BuildIdentity(DeclaredFamily, affix="BuildIdentity"):
    @abstractmethod
    def recorded(self) -> RecordedBuildIdentity: ...

    @abstractmethod
    def require_installed(self) -> None: ...


@dataclass(frozen=True)
class UnrecordedBuildIdentity(BuildIdentity):
    def recorded(self) -> RecordedBuildIdentity:
        return RecordedBuildIdentity.installed()

    def require_installed(self) -> None:
        installed = RecordedBuildIdentity.installed()
        raise ProofBuildMismatchError(
            f"Proof build is unrecorded; installed Core {installed.core}, "
            f"Toad {installed.toad}; run and record a new proof."
        )


@dataclass(frozen=True)
class RecordedBuildIdentity(BuildIdentity):
    """The installed pair, observed from its interpreter rather than this caller's imports."""

    core: Annotated[str, GitCommitText]
    toad: Annotated[str, GitCommitText]

    def recorded(self) -> RecordedBuildIdentity:
        return self

    @classmethod
    def installed(cls) -> RecordedBuildIdentity:
        runtime = LaunchRuntime.installed()
        runtime.require_available()
        # The installer records both source commits for this exact runtime. A wheel's
        # direct_url.json names its local archive, not the commit it was built from.
        build = InstalledBuildManifest.model_validate_json(
            (runtime.prefix / "build.json").read_bytes()
        )
        return FieldCodec.decode(
            cls,
            {"kind": cls.declared_name, "core": build.core_commit, "toad": build.toad_commit},
        )

    def require_installed(self) -> None:
        installed = self.installed()
        if self != installed:
            raise ProofBuildMismatchError(
                f"Proof build Core {self.core}, Toad {self.toad} differs from installed "
                f"Core {installed.core}, Toad {installed.toad}; run the proof again."
            )


class RestoredRuntime(LaunchRuntime):
    """Original-process recovery preserves its original import environment too."""

    def _environment(self, source: Mapping[str, str]) -> dict[str, str]:
        return dict(source)


class ImportPath(DeclaredFamily, JsonShapeFamily, affix="ImportPath"):
    """An inherited Python search path or its explicit removal."""


@dataclass(frozen=True)
class NoImportPath(ImportPath, JsonShapeMember):
    value: None = None


@dataclass(frozen=True)
class SpecifiedImportPath(ImportPath, JsonShapeMember):
    value: str


class EnvironmentValue(SubclassUnion, affix="Variable"):
    """One inherited environment variable: its text, or unset."""

    @abstractmethod
    def otherwise[Default](self, default: Default) -> str | Default:
        """The variable's text; an unset variable takes the reader's own default."""


@dataclass(frozen=True, slots=True)
class SetVariable(EnvironmentValue):
    text: str

    @classmethod
    def from_wire(cls, data: object) -> Self:
        return cls(FieldCodec.decode(str, data))

    def to_wire(self) -> str:
        return self.text

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.value_schema(str)

    def otherwise[Default](self, default: Default) -> str:
        return self.text


@dataclass(frozen=True, slots=True)
class UnsetVariable(NullMember, EnvironmentValue):
    def otherwise[Default](self, default: Default) -> Default:
        return default


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
        """Observe this owner's credentials, never an ambient or fork directory."""
        info = (self.native_config / "auth.json").stat()
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
        EnvironmentValue, WireOmitDefault(), WireName("PI_PARENT_ID")
    ] = UnsetVariable()
    task: Annotated[
        EnvironmentValue, WireOmitDefault(), WireName("PI_TASK")
    ] = UnsetVariable()
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
            UnsetVariable() if thread.parent is None else SetVariable(thread.parent),
            # The task's wire text is this variable's; thread_identity imports this module.
            FieldCodec.decode(EnvironmentValue, FieldCodec.encode(thread.task)),
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
