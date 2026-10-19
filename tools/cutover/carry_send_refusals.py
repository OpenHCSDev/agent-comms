"""Add refused-send outcomes to the existing delivery database without changing saved rows."""

from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordinator import StandaloneCoordination
from agent_comms.notification_assignment import NotificationAssignment
from agent_comms.coordination_tables.refusals import SendRefusal

from .carry import Carried, Carry


class SendRefusalOutcomes(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return (NotificationAssignment.database_path(root),)

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = NotificationAssignment.database_path(root)
        if not path.exists():
            return Carried(0)
        with CoordinationStore.observing(path) as db:
            actual = SendRefusal.stored_schema(db)
        if actual:
            SendRefusal.require_schema(actual)
            return Carried(0)
        if apply:
            with StandaloneCoordination(str(path)) as store:
                SendRefusal.install(store)
        return Carried(1, {"tables": 1}, (path.name,) if apply else ())


if __name__ == "__main__":
    SendRefusalOutcomes.main()
