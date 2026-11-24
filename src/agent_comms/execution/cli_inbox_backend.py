"""External CLI-inbox execution as a real AgentBackend.

CLI-inbox threads are driven by external CLI/MCP sessions (Claude Code, Codex,
Antigravity, etc.) through Core's inbox watch and external transcript store rather
than a Core-launched Pi RPC child. Declaring ``CliInboxBackend`` here puts CLI
inboxes under the same ``AgentBackend`` capability hierarchy (``Forks``,
``RuntimeLocation``) as ``PiNativeBackend`` and ``PiDurableBackend``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from agent_comms.agent_backend import (
    AgentBackend,
    BackendDeclarationError,
    FailedCompaction,
    Forks,
    NewThreadBackend,
    RuntimeLocation,
)
from agent_comms.execution.history_view import ForkOwnHistory
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.transcript_sources import ExternalTranscript
from agent_comms.nominal.field_codec import AcpSessionId
from agent_comms.transcript.no_native_transcript_cursor import NoNativeTranscriptCursor


@dataclass(frozen=True)
class CliInboxRuntime(RuntimeLocation):
    """Runtime location for a CLI-inbox thread.

    ``path`` is empty for a pure CLI inbox, or holds a preserved native Pi session
    path retained across a ``NativeThread`` -> ``ExternalThread`` switch so delete
    still frees the file and switching back can resume it. External transcript items
    always remain the thread's active history source while on this backend.
    """

    path: str = ""

    def is_resumable(self) -> bool:
        return False

    def require_forkable(self) -> None:
        pass

    def last_written(self) -> float:
        return 0.0

    async def compact(self, operation):
        return FailedCompaction("A CLI-inbox thread compacts in its own CLI session.")

    def new_location(self, root, worktree) -> CliInboxRuntime:
        return CliInboxRuntime()

    def fork_session_path(self, root: Path, worktree: Path) -> tuple[type[AgentBackend], str]:
        native = NewThreadBackend.selected()
        return (native, native.new_location(root, worktree).read_key)

    def arguments(self):
        return {}

    def read_keys(self):
        return ()

    def transcript_cursor(self, frontier, cut, *, source):
        return NoNativeTranscriptCursor(
            source=source, offset=cut.offset, wire=frontier, file=cut.file_identity(),
        )

    def history_locations(self, thread, registry, visited):
        yield from thread.inherited_history(registry, visited)
        yield self, thread.parent.own_history

    def view_from_fork(self, view, fork_birth):
        return ForkOwnHistory()

    def external_records(self, root, incarnation):
        return self.history_source(root, incarnation)

    def history_source(self, root, incarnation):
        return ExternalTranscript(root, incarnation)

    def storage_files(self):
        if not self.path:
            return ()
        return NewThreadBackend.selected().location_type(self.path).storage_files()

    @property
    def read_key(self) -> str | None:
        return None


class CliInboxBackend(Forks, AgentBackend):
    """External CLI/MCP inbox backend: reads/publishes external transcripts and forks directly."""

    location_type = CliInboxRuntime
    event_kinds = frozenset()

    @classmethod
    def for_worker(cls, runner, session_id: AcpSessionId) -> Self:
        raise BackendDeclarationError(
            "CliInboxBackend runs in an external CLI session, not inside a native turn worker."
        )

    @classmethod
    def new_location(cls, root, worktree) -> CliInboxRuntime:
        return CliInboxRuntime()

    @classmethod
    def unstarted_location(
        cls, fallback: Callable[[type[AgentBackend]], RuntimeLocation],
    ) -> RuntimeLocation:
        return CliInboxRuntime()

    @classmethod
    def registered_over(cls, previous: type[AgentBackend]) -> type[AgentBackend]:
        return cls

    @classmethod
    def as_native_backend(cls, incoming: type[AgentBackend]) -> type[AgentBackend]:
        return incoming

    @classmethod
    async def fork(
        cls, parent, launcher: str, runtime: LaunchRuntime, root: Path,
    ) -> RuntimeLocation:
        """A CLI-inbox fork inherits its parent's transcript history through ForkedParent."""
        return CliInboxRuntime()
