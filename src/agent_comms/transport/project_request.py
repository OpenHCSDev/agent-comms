from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Self

from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.thread_presentation import LiveThreadOwnerBinding
from agent_comms.registry.threads import Thread
from agent_comms.transport.owner_connection import socket_path
from agent_comms.transport.runtime_request import AttachedResultRuntimeRequest


@dataclass(frozen=True, kw_only=True)
class ProjectRuntimeRequest(AttachedResultRuntimeRequest):
    """Current project observation through the original native launch binding.

    This command grants no input or tool execution. A retained native process
    must still observe its original registry lease before each project check.
    """
    binding: LiveThreadOwnerBinding

    @classmethod
    def for_native(cls, snapshot: RegistrySnapshot, thread: Thread) -> Self:
        process = thread.require_process()
        owner = snapshot.owner_identity(thread.name)
        snapshot.require_owner_process(owner, process)
        return cls(thread=thread.name, binding=LiveThreadOwnerBinding(owner, process))

    def environment(self, root):
        return {
            "AGENT_COMMS_PROJECT_SOCKET": str(socket_path(root, self.binding.process.pid)),
            "AGENT_COMMS_PROJECT_REQUEST": json.dumps(FieldCodec.encode(self)),
        }

    @classmethod
    def native_environment(
        cls, thread: Thread, root: Path, snapshot: RegistrySnapshot,
    ) -> dict[str, str]:
        """Project this captured owner through the original native binding."""
        project = cls.for_native(snapshot, thread)
        return {
            **replace(thread.launch(), managed=True).encode(),
            **project.environment(root),
        }
