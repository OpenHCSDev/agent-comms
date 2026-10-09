"""Original process launch capture and the declared target runtime projection.

Credentials remain in memory for one fenced handoff, never in a restart record.
Linux /proc is decoded here once; the lifecycle and queued watcher share it.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Annotated, ClassVar

from .child_process import Platform, ProcessIdentity
from .errors import RelationViolationError
from .field_codec import FieldCodec, PathText
from .private_nk_entrypoint import PACKAGE_ENV, ROOT_ID_ENV
from .registry_document import RegistrySnapshot
from .threads import Thread


@dataclass(frozen=True)
class RestartEnvironment:
    """Declared inheritance policy for the credential-free resident watcher.

    Private launch variable spellings belong to PrivateNkLaunch. Owner credentials
    stay in /proc and are read only from the exact selected process at execution.
    """

    home: str | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "HOME"}
    )
    path: str | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_name": "PATH", "runtime": True}
    )
    pythonpath: str | None = field(
        default=None,
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
    binary_key: ClassVar[str] = "AGENT_COMMS_AGENT_BIN"
    arguments_key: ClassVar[str] = "AGENT_COMMS_AGENT_ARGS"

    def __post_init__(self) -> None:
        object.__setattr__(self, "native_config", self.absolute(self.native_config))
        object.__setattr__(self, "agent_directory", self.absolute(self.agent_directory))

    def absolute(self, directory: Path) -> Path:
        if directory.parts and directory.parts[0] == "~":
            directory = Path(self.home or Path.home()).joinpath(*directory.parts[1:])
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

    def settings_paths(self, worktree: Path) -> tuple[str, ...]:
        project = worktree / ".pi"
        return tuple(dict.fromkeys(map(str, (
            self.agent_directory / "settings.json", project / "settings.json",
            self.agent_directory / "models.json", self.native_config / "models.json",
            project / "models.json",
        ))))

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


@dataclass(frozen=True, slots=True)
class RetainedOwnerLaunch:
    """An immutable original process observation, not owner/admission authority."""

    process: ProcessIdentity
    interpreter: str
    environment: dict[str, str] = field(repr=False)
    configuration: RestartEnvironment = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "configuration", RestartEnvironment.inherit(self.environment))

    @classmethod
    def capture(
        cls, thread: Thread, snapshot: RegistrySnapshot, *, interpreter: str | None = None
    ) -> RetainedOwnerLaunch:
        process = thread.require_process()
        platform = Platform.current()
        platform.require(process)
        command = Path(f"/proc/{process.pid}/cmdline").read_bytes().split(b"\0", 1)[0]
        observed_interpreter = os.fsdecode(command)
        if not observed_interpreter or (
            interpreter is not None and observed_interpreter != interpreter
        ):
            raise RelationViolationError("Selected source owner interpreter changed")
        raw = Path(f"/proc/{process.pid}/environ").read_bytes()
        values = dict(item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
        environment = {os.fsdecode(key): os.fsdecode(value) for key, value in values.items()}
        platform.require(process)
        route = environment.get(RestartEnvironment.owner_key)
        if not route or not environment.get(RestartEnvironment.binary_key):
            raise RelationViolationError("Owner launch configuration cannot be verified")
        # A worker's launch name may have been renamed since exec. The original
        # snapshot owns that relation; a PID or spelling match cannot grant it.
        if snapshot.owner_identity(route) != snapshot.owner_identity(thread.name):
            raise RelationViolationError("Owner launch names another registry incarnation")
        snapshot.require_owner_process(snapshot.owner_identity(thread.name), process)
        result = cls(process, observed_interpreter, environment)
        result.arguments  # Decode/validate malformed shell arguments before any fence.
        return result

    @property
    def binary(self) -> str:
        return self.environment[RestartEnvironment.binary_key]

    @property
    def arguments(self) -> tuple[str, ...] | None:
        arguments = self.environment.get(RestartEnvironment.arguments_key)
        return tuple(shlex.split(arguments)) if arguments is not None else None
