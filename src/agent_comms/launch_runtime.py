"""Selected process runtimes and their declared launch environment."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Annotated, ClassVar

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, JsonShapeFamily, JsonShapeMember, PathText

ROOT_ID_ENV = "AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID"
PACKAGE_ENV = "AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE"


class CallingThreadUnavailable(RelationViolationError):
    """The process environment does not declare its owner thread."""


@dataclass(frozen=True)
class LaunchRuntime:
    """One selected installation for a worker and its native executable."""

    interpreter: Annotated[Path, PathText]

    @classmethod
    def current(cls) -> LaunchRuntime:
        # Resolve the directory, not Python's symlink into the shared interpreter.
        selected = cls(cls.current_pointer().resolve(strict=True) / "bin/python")
        selected.require_available()
        return selected

    @staticmethod
    def current_pointer() -> Path:
        return Path.home() / ".local/share/agent-comms/current"

    @classmethod
    def running(cls) -> LaunchRuntime:
        """Identify the decoder already running, for watcher checks and restoration."""
        return cls(Path(sys.executable))

    @property
    def native_entrypoint(self) -> str:
        return str(self.interpreter.with_name("pi-comms-native"))

    def environment(self, source: Mapping[str, str]) -> dict[str, str]:
        configuration = replace(
            RestartEnvironment.inherit(source).for_interpreter(self.interpreter).without_pythonpath(),
            virtual_env=str(self.interpreter.parent.parent),
        )
        return configuration.apply_runtime(source)

    def require_available(self) -> None:
        if not self.interpreter.is_file() or not os.access(self.interpreter, os.X_OK):
            raise ValueError("Target interpreter is unavailable")


class RestoredRuntime(LaunchRuntime):
    """Original-process recovery preserves its original import environment too."""

    def environment(self, source: Mapping[str, str]) -> dict[str, str]:
        return dict(source)


class ImportPath(DeclaredFamily, JsonShapeFamily, affix="ImportPath"):
    """An inherited Python search path or its explicit removal."""


@dataclass(frozen=True)
class NoImportPath(ImportPath, JsonShapeMember):
    value: None = None


@dataclass(frozen=True)
class SpecifiedImportPath(ImportPath, JsonShapeMember):
    value: str


@dataclass(frozen=True)
class RestartEnvironment:
    """Declared inheritance policy for the credential-free resident watcher.

    Launch variable spellings belong to this environment declaration. Owner credentials
    stay in /proc and are read only from the exact selected process at execution.
    """

    home: str | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "HOME"}
    )
    path: str | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "PATH", "runtime": True}
    )
    pythonpath: ImportPath = field(
        default_factory=NoImportPath,
        metadata={"wire_omit_default": True, "wire_name": "PYTHONPATH", "runtime": True},
    )
    virtual_env: str | None = field(
        default=None,
        metadata={"wire_omit_default": True, "wire_name": "VIRTUAL_ENV", "runtime": True},
    )
    config: str | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "XDG_CONFIG_HOME"}
    )
    root: str | None = field(
        default=None,
        metadata={"wire_omit_default": True, "wire_name": "AGENT_COMMS_ROOT", "runtime": True},
    )
    root_id: str | None = field(
        default=None,
        metadata={"wire_omit_default": True, "wire_name": ROOT_ID_ENV, "runtime": True},
    )
    package: str | None = field(
        default=None,
        metadata={"wire_omit_default": True, "wire_name": PACKAGE_ENV, "runtime": True},
    )
    default_agent_directory: ClassVar[Path] = Path("~/.pi/agent")
    native_config: Annotated[Path, PathText] = field(
        default=default_agent_directory,
        metadata={"wire_name": "AGENT_COMMS_NATIVE_CONFIG_DIR", "native": True},
    )
    agent_directory: Annotated[Path, PathText] = field(
        default=default_agent_directory,
        metadata={"wire_name": "PI_CODING_AGENT_DIR", "native": True},
    )
    owner_key: ClassVar[str] = "AGENT_COMMS_THREAD"

    @classmethod
    def calling_thread(cls, environment: Mapping[str, str] = os.environ) -> str:
        caller = environment.get(cls.owner_key)
        if not caller:
            raise CallingThreadUnavailable(
                f"Calling thread requires {cls.owner_key}."
            )
        return caller

    @classmethod
    def called_by(
        cls,
        name: str,
        canonicalize: Callable[[str], str],
        *,
        environment: Mapping[str, str] = os.environ,
    ) -> bool:
        caller = environment.get(cls.owner_key)
        return caller is not None and canonicalize(caller) == name

    def __post_init__(self) -> None:
        object.__setattr__(self, "native_config", self.absolute(self.native_config))
        object.__setattr__(self, "agent_directory", self.absolute(self.agent_directory))

    def absolute(self, directory: Path) -> Path:
        if directory.is_relative_to("~"):
            directory = Path(self.home or Path.home()) / directory.relative_to("~")
        return directory.expanduser().resolve()

    @classmethod
    def inherit(cls, environment: Mapping[str, str]):
        values = {f.metadata["wire_name"]: environment[f.metadata["wire_name"]]
                  for f in fields(cls) if f.metadata["wire_name"] in environment}
        native_name = cls.__dataclass_fields__["native_config"].metadata["wire_name"]
        agent_name = cls.__dataclass_fields__["agent_directory"].metadata["wire_name"]
        values[native_name] = values.get(native_name) or values.get(agent_name) or str(cls.default_agent_directory)
        return FieldCodec.decode(cls, values)

    def for_agent(self, directory: Path) -> RestartEnvironment:
        """Change only the acquired writable resource, retaining original discovery."""
        return replace(self, agent_directory=directory)

    def without_pythonpath(self) -> RestartEnvironment:
        return replace(self, pythonpath=NoImportPath())

    def for_interpreter(self, interpreter: Path) -> RestartEnvironment:
        """Put this process's runtime first, exactly once; preserve other search paths."""
        directory = str(interpreter.parent)
        inherited = os.defpath if self.path is None else self.path
        path = os.pathsep.join((directory, *(entry for entry in inherited.split(os.pathsep)
                                            if entry != directory)))
        return replace(self, path=path)

    def encode_native(self) -> dict[str, str]:
        encoded = self.encode()
        return {f.metadata["wire_name"]: encoded[f.metadata["wire_name"]]
                for f in fields(self) if f.metadata.get("native")}

    def auth_revision(self) -> tuple[int, int]:
        """Observe this owner's credentials, never an ambient or fork directory."""
        try:
            info = (self.native_config / "auth.json").stat()
        except FileNotFoundError:
            return 0, 0  # No credentials file yet.
        return info.st_mtime_ns, info.st_size

    def encode(self) -> dict[str, str]:
        return FieldCodec.encode(self)

    def apply_runtime(self, source: Mapping[str, str]) -> dict[str, str]:
        """Preserve source credentials/settings; replace the declared runtime fields."""
        result = dict(source)
        encoded = self.encode()
        for declaration in fields(self):
            if declaration.metadata.get("runtime"):
                name = declaration.metadata.get("wire_name")
                result.pop(name, None)
                if name in encoded:
                    result[name] = encoded[name]
        return result
