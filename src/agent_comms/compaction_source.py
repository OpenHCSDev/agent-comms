"""Pre-summary source identity, captured only under the real owner/writer boundary."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .errors import RelationViolationError
from .field_codec import FieldCodec, projected
from .owner_compaction_prepare import NativeWitness

if TYPE_CHECKING:
    from .compaction_boundary import HeldCompaction

@dataclass(frozen=True)
class CompactionSource:
    """Pre-summary observations, never authority or an accepted JSON receipt."""

    native: NativeWitness = field(metadata={"journal_exclude": True})
    wire_root: str
    thread: str
    owner_generation: int = field(metadata={"wire_name": "owner_epoch"})
    turn_id: str
    goal_id: str | None
    goal_revision: int | None
    bus_revision: str
    input_revision: str
    pending_input_key: str | None = None
    settings_paths: tuple[str, ...] | None = None
    settings_revision: tuple[str, ...] | None = None

    @projected(view="journal", name="native_json")
    def encoded_native(self) -> str:
        return json.dumps(FieldCodec.encode(self.native), sort_keys=True, separators=(",", ":"))


    def require_current(self, held: HeldCompaction) -> None:
        if self != held.capture(self.pending_input_key, self.settings_paths):
            raise RelationViolationError("Compaction source changed; derive fresh evidence")
