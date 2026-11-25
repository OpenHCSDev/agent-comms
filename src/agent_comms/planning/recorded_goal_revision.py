from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import WireOmitDefault
from agent_comms.planning.goals import GoalDeclaration
from agent_comms.planning.owner_requests import NoRequestRevision, RequestRevision
from agent_comms.storage.typed_table import Column, JsonStorage


class RevisionKind(DeclaredFamily, affix="Revision"):
    """The declared reason a goal revision entered the journal."""


@dataclass(frozen=True, kw_only=True)
class RequestHistory:
    """Moved request data stays linked to its original history observation."""

    request_before: Annotated[
        RequestRevision, Column(storage=JsonStorage, nullable=True), WireOmitDefault(),
    ] = NoRequestRevision()
    request_after: Annotated[
        RequestRevision, Column(storage=JsonStorage, nullable=True), WireOmitDefault(),
    ] = NoRequestRevision()


@dataclass(frozen=True)
class RecordedGoalRevision(RequestHistory):
    """A history entry's public fields; its owner key and journal state stay in the store."""
    sequence: Annotated[int, Column(primary_key=True)]
    kind: type[RevisionKind]
    observed_at: float
    before: Annotated[GoalDeclaration, Column(storage=JsonStorage, nullable=True)]
    after: Annotated[GoalDeclaration, Column(storage=JsonStorage, nullable=True)]
