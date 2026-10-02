"""Pre-summary source identity, captured only under the real owner/writer boundary."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from .errors import RelationViolationError
from .input_attempt import StoredInput
from .field_codec import FieldCodec, projected
from .owner_compaction_prepare import NativeWitness
from .retained_task_facts import RetainedTaskFacts

if TYPE_CHECKING:
    from .compaction_boundary import HeldCompaction
    from .compaction_states import CommittedNativeOutcome

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
    retained: RetainedTaskFacts
    pending_inputs: tuple[StoredInput, ...]
    settings_paths: tuple[str, ...] | None = None
    settings_revision: tuple[str, ...] | None = None

    @property
    def pending_input_keys(self) -> tuple[str, ...]:
        return tuple(row.key for row in self.pending_inputs)

    @projected(view="journal", name="native_json")
    def encoded_native(self) -> str:
        return json.dumps(FieldCodec.encode(self.native), sort_keys=True, separators=(",", ":"))


    def require_current(self, held: HeldCompaction) -> None:
        if self != held.capture(self.pending_input_keys, self.settings_paths):
            raise RelationViolationError("Compaction source changed; derive fresh evidence")

    def at_prepared_cut(self, witness: NativeWitness) -> CompactionSource:
        """Allocate a cut for these exact facts without changing their source.

        Preparation may move only first_kept_entry_id. Original session, leaf
        and revision remain fenced; the caller must require_current before use.
        """
        if replace(witness, first_kept_entry_id=self.native.first_kept_entry_id) != self.native:
            raise RelationViolationError("Compaction allocation changed its original native source")
        return replace(self, native=witness)

    def after_native_commit(self, outcome: CommittedNativeOutcome) -> CompactionSource:
        """Advance only this original cut through its committed native receipt.

        The containing operation owns the receipt. Its caller must still hold
        writer/owner/input custody and require_current on the returned source;
        this projection grants neither input admission nor replay. Every other
        original fact remains subject to the same equality fence.
        """
        return replace(self, native=replace(
            self.native, leaf_id=outcome.leaf_id, revision=outcome.revision,
        ))
