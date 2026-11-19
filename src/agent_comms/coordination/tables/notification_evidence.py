"""One cheap revision for the coordination rows notification display actually reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.coordination.schema import CoordinatorRow
from agent_comms.storage.typed_table import (
    Column,
    FrozenColumn,
    MutationCapability,
    RowMutation,
    SQLiteSchemaObject,
    WithoutRowid,
)


@dataclass(frozen=True)
class NotificationEvidenceRevision(CoordinatorRow, WithoutRowid):
    """Monotone revision of only the rows used by notification projections."""

    singleton: Annotated[int, FrozenColumn(primary_key=True, check="singleton = 1")] = 1
    revision: Annotated[int, Column(check="revision > 0")] = 1


class BumpsNotificationEvidenceRevision(MutationCapability):
    """A row read by notification display advances its shared revision after mutation."""

    @classmethod
    def mutation_guards(cls, row) -> tuple[SQLiteSchemaObject, ...]:
        revision = NotificationEvidenceRevision.declared_name
        result = []
        for mutation in RowMutation:
            name = f"{row.declared_name}_{mutation.value.lower()}_after_notification_revision"
            result.append(SQLiteSchemaObject(
                name,
                f'CREATE TRIGGER "{name}" AFTER {mutation.value} ON "{row.declared_name}" '
                f'BEGIN INSERT INTO "{revision}" (singleton, revision) VALUES (1, 1) '
                'ON CONFLICT(singleton) DO UPDATE SET revision=revision+1; END',
            ))
        return tuple(result)
