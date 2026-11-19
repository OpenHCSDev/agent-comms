from __future__ import annotations

import socket
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, ExitStack
from dataclasses import replace
from typing import TextIO

from agent_comms.execution.child_process import (
    OutlivesLauncherLifetime,
    ProcessIdentity,
    SpawnedProcess,
)
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.root_runtime import (
    HostRetirement,
    NoRootHost,
    RecordedRootHost,
    RootHost,
    RootRuntime,
    RootRuntimeStore,
)
from agent_comms.installer.native_build_inputs import DetachedChildStdio
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.recorded_bus import RecordedBus
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.storage.file_observation import FileIdentity
from agent_comms.storage.locked_store import DocumentChange
from agent_comms.storage.private_path import PrivateSocketRole
from agent_comms.transport.owner_connection import socket_path


class RootHostStore(RootRuntimeStore, HostRetirement, declared_name=None):
    def retire_host(self) -> RootHost:
        """Retire this root's physical host only after its logical owners detached."""
        return self.read().host.retired_through(self)

    def host_retired_once_ownerless(self) -> RootHost:
        """Keep the registry cut current through the host write: no new owner may
        attach between the ownership decision and the physical retirement.
        """
        with RegistryStore.at(self.path.parent).current_snapshot() as cut:
            def retire(data: RootRuntime) -> DocumentChange[RootRuntime, RootHost]:
                if data.host.retire(
                    self.path.parent, lambda process: not cut.snapshot.process_owners(process),
                ):
                    data = replace(data, host=NoRootHost())
                return DocumentChange(data, data.host)
            return self.update(retire)

    def acquire_host(
        self, runtime: LaunchRuntime, command: tuple[str, ...], *,
        environment: Mapping[str, str], cwd: str,
        output: Callable[[], AbstractContextManager[TextIO]],
        room_for_start: Callable[[], AbstractContextManager[None]],
    ) -> ProcessIdentity:
        """Publish the root's one host while its existing exec gate is still closed."""
        observed = self.read().available_host(self.path.parent, runtime)
        if observed.alive():
            return observed.require("Recorded worker host has no process")
        # Wait for memory without holding either writer.
        # Re-read under the writer before launch so concurrent starters share one host.
        # The launch remains gated until update has published the winning host.
        with room_for_start(), ExitStack() as resources:
            def launch(current: RootRuntime) -> DocumentChange[RootRuntime, ProcessIdentity]:
                observed = current.available_host(self.path.parent, runtime)
                if observed.alive():
                    return DocumentChange(current, observed.require("Recorded worker host has no process"))
                capture = resources.enter_context(output())
                listener = resources.enter_context(socket.socket(socket.AF_UNIX))
                child = resources.enter_context(SpawnedProcess.launching(
                    (*command, "--listener-fd", str(listener.fileno())),
                    env=environment, cwd=cwd, stdio=DetachedChildStdio(stdout=capture), runtime=runtime,
                    pass_fds=(listener.fileno(),), lifetime=OutlivesLauncherLifetime,
                ))
                path = socket_path(self.path.parent, child.identity.pid)
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with PrivateSocketRole.address(path) as address:
                    listener.bind(str(address))
                listener.listen()
                bus = RecordedBus(FileIdentity.from_stat(Bus.path_in(self.path.parent).stat()))
                return DocumentChange(replace(current, host=RecordedRootHost(child.identity, bus)), child.identity)
            return self.update(launch)
