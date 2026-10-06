"""Original report preservation owns this one-shot member retirement.

Only a source-declaration-decoded carrier with acquired committed goal history
can produce the target postimage. No ordinary reader accepts the retired field.
"""
from dataclasses import dataclass
from pathlib import Path

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_history import GoalHistoryEntry, GoalHistoryStore
from agent_comms.owner_lifecycle import OwnerReleaseReceipt
from agent_comms.registry_document import RegistryDocument
from agent_comms.threads import Thread


@dataclass(frozen=True)
class GoalReportMemberRetirement:
    registry: RegistryDocument
    releases: dict[str, OwnerReleaseReceipt]
    history: tuple[GoalHistoryEntry, ...]

    @classmethod
    def acquire(cls, registry_path: Path, registry: RegistryDocument,
                releases: dict[str, OwnerReleaseReceipt]):
        """Caller retains the original registry/release locks throughout use."""
        acquired = cls(registry, releases, GoalHistoryStore.acquire_read_only(registry_path))
        acquired.require_preserved()
        return acquired

    def require_carrier(self, thread: Thread, *, current: bool) -> None:
        """Match this original carrier to its own incarnation's full history."""
        rows = tuple(row for row in self.history if row.owner_created_at == thread.created_at)
        if any(row.state in ('pending', 'uncertain') for row in rows):
            raise RelationViolationError('Original goal report has unresolved journal evidence')
        committed = tuple(row for row in rows if row.state == 'committed')
        goal = None
        report = None
        matched = thread.goal is None and thread.last_goal_report_turn is None
        for row in committed:
            if row.kind == 'observed_gap' or row.before != goal:
                raise RelationViolationError('Original goal report history has a gap or conflict')
            if row.kind == 'baseline' and row.after is not None and row.after.reported_turn is not None:
                raise RelationViolationError('A baseline cannot attest an original model report')
            if (row.kind == 'transition' and row.after is not None
                    and row.after.reported_turn is not None
                    and row.after.reported_turn != (row.before.reported_turn if row.before else None)):
                report = row.after.reported_turn
            goal = row.after
            if goal == thread.goal and report == thread.last_goal_report_turn:
                matched = True
        if not matched or (current and (goal != thread.goal or report != thread.last_goal_report_turn)):
            raise RelationViolationError('Original goal report is missing or conflicts with its carrier')

    def require_preserved(self) -> None:
        for thread in self.registry.threads.values():
            self.require_carrier(thread, current=True)
        for receipt in self.releases.values():
            self.require_carrier(receipt.thread, current=False)

    def _postimage(self, thread: Thread) -> dict:
        # Only reachable after all source carriers were validated together.
        target = FieldCodec.encode(thread)
        del target['last_goal_report_turn']
        return target

    def project(self) -> dict:
        self.require_preserved()
        registry = FieldCodec.encode(self.registry)
        registry['threads'] = {name: self._postimage(thread)
                               for name, thread in self.registry.threads.items()}
        releases = {name: dict(FieldCodec.encode(receipt), thread=self._postimage(receipt.thread))
                    for name, receipt in self.releases.items()}
        return {'registry': registry, 'releases': releases,
                'goal_history': FieldCodec.encode(self.history)}
