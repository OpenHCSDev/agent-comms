"""The runtime location before a backend has acquired saved state."""

from __future__ import annotations

from dataclasses import dataclass

from .agent_backend import AgentBackend, FailedCompaction, RuntimeLocation
from .external_transcript import ExternalTranscript
from .history_view import ForkOwnHistory


@dataclass(frozen=True)
class UnstartedRuntime(RuntimeLocation):
    owner: type[AgentBackend]
    resumable = False

    @property
    def backend(self):
        return self.owner

    @property
    def path(self) -> str:
        raise ValueError("Canonical saved session required")

    def require_forkable(self):
        raise ValueError("Canonical saved session required")

    def last_written(self) -> float:
        """No saved session exists to have grown."""
        return 0.0

    async def compact(self, operation):
        return FailedCompaction("This thread has no saved session to compact.")

    def new_location(self, root, worktree):
        return self.owner.new_location(root, worktree)

    def arguments(self):
        return {}

    def read_keys(self):
        return ()

    def transcript_cursor(self, frontier, offset, *, source):
        from .transcripts import NoNativeTranscriptCursor
        return NoNativeTranscriptCursor(source=source, offset=offset, wire=frontier)

    def history_locations(self, thread, registry, visited):
        yield from thread.inherited_history(registry, visited)
        yield self, thread.parent.own_history

    def view_from_fork(self, view, fork_birth):
        """history_source reads the asking thread's own records, whichever thread this location
        came from. A fork's own work is all after its birth, so no date can filter it."""
        return ForkOwnHistory()

    def external_records(self, root, incarnation):
        return self.history_source(root, incarnation)

    def select_session(self, directory, execution, source_reads, *, participant):
        location = participant.comms.registry.claim_fresh_session(participant)
        return location.select_session(
            directory, execution, source_reads, participant=participant,
        )

    def history_source(self, root, incarnation):
        return ExternalTranscript(root, incarnation)

    def storage_files(self):
        return ()

    @property
    def read_key(self) -> str | None:
        return None
