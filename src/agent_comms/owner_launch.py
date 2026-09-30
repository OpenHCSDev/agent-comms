"""Original process launch capture and the declared target runtime projection.

Credentials remain in memory for one fenced handoff, never in a restart record.
Linux /proc is decoded here once; the lifecycle and queued watcher share it.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import ClassVar

from .child_process import Platform, ProcessIdentity
from .errors import RelationViolationError
from .field_codec import FieldCodec
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
    owner_key: ClassVar[str] = "AGENT_COMMS_THREAD"
    binary_key: ClassVar[str] = "AGENT_COMMS_AGENT_BIN"
    arguments_key: ClassVar[str] = "AGENT_COMMS_AGENT_ARGS"

    @classmethod
    def inherit(cls, environment: Mapping[str, str]):
        return cls(**{f.name: environment.get(f.metadata["wire_name"]) for f in fields(cls)})

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
    environment: Mapping[str, str] = field(repr=False)

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
