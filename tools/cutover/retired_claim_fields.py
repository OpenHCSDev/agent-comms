"""Retired field-name conversion shared by the carry and physical JSONL read-back.

This capability owns only the predecessor names. FieldCodec owns wire traversal.
Delete with the claim-field carry after all retained roots cross the format change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache

from agent_comms.acp_extension import LiveCursorScope, LiveQueueScope
from agent_comms.envelope_claim_transitions import AcquiringClaim, RecordedScope
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, compile_decoder
from agent_comms.goal_failure_observation import FailedTurnEvidence
from agent_comms.historical_views import HistoryArchive
from agent_comms.input_attempt import StoredInput
from agent_comms.input_origin import HumanInputOrigin
from agent_comms.locked_store import JsonDocument, RegistryFile, SQLiteFile
from agent_comms.native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.queued_input import QueuedInputContext
from agent_comms.registry_document import RegistryDocument
from agent_comms.restart_queue import QueuedRestart
from agent_comms.selected_write_plan import (
    SelectedWriteBinding,
    SelectedWriteIntent,
    SelectedWritePlans,
)
from agent_comms.thread_identity import ClaimedIdentity, TurnFence
from agent_comms.turn_lease import ActiveTurn
from agent_comms.typed_table import TypedTable, _identifier

from .archived_roots import ArchivedCarry


@dataclass(frozen=True)
class RenamedField:
    owner: type
    name: str
    old: str


# These are the retired declarations. Current spelling, JSON layout, SQL storage,
# descendants and consumers all come from FieldCodec/TypedTable, not this list.
RENAMED = (
    RenamedField(LiveCursorScope, "claimed_identity", "admission"),
    RenamedField(LiveQueueScope, "claimed_identity", "admission"),
    RenamedField(HumanInputOrigin, "claimed_identity", "admission"),
    RenamedField(QueuedInputContext, "claimed_identity", "admission"),
    RenamedField(SelectedWriteBinding, "claimed_identity", "admission"),
    RenamedField(AcquiringClaim, "resource_scope", "admission"),
    RenamedField(RecordedScope, "owner_claimed_generation", "owner_admission_generation"),
    RenamedField(StoredInput, "claim_generation", "admission"),
    RenamedField(FailedTurnEvidence, "claim_generation", "admission"),
    RenamedField(ClaimedIdentity, "claimed_generation", "admission_generation"),
    RenamedField(TurnFence, "claimed_generation", "admission_generation"),
    RenamedField(ActiveTurn, "claimed_generation", "admission_generation"),
    RenamedField(OwnerRestartSelection, "claimed_generation", "admission_generation"),
    RenamedField(SelectedWriteIntent, "claim_epoch", "admission_epoch"),
    RenamedField(RegistryDocument, "claims", "admissions"),
    RenamedField(NativeRuntimeInput, "sent_owner_claimed_generation", "sent_owner_admission_generation"),
    RenamedField(CurrentNativeCursor, "owner_claimed_generation", "owner_admission_generation"),
)


class RetiredClaimFields(ArchivedCarry):
    opened_files = (SQLiteFile, JsonDocument, RegistryFile, HistoryArchive,
                    SelectedWritePlans, QueuedRestart)
    @classmethod
    @cache
    def retired_fields(cls, record: type) -> dict[str, str]:
        return {item.name: item.old for item in RENAMED if issubclass(record, item.owner)}

    @classmethod
    @cache
    def retired_keys(cls) -> tuple[str, ...]:
        """Each old spelling as quoted JSON text. Conservative: text holding none of
        them has no key rename_record could change, so a reader skips it undecoded."""
        return tuple(sorted({json.dumps(item.old) for item in RENAMED}))

    @classmethod
    def selects_record(cls, record: type) -> bool:
        return bool(cls.retired_fields(record))

    @classmethod
    def retired_columns(cls, record: type[TypedTable], stored: tuple[str, ...]) -> dict[str, str]:
        fields = record._fields
        changed = {field.name: _identifier(old)
                   for field in fields if field.column.record_key
                   and (old := cls.retired_fields(record).get(field.name)) in stored}
        if any(field.wire_name in stored for field in fields if field.name in changed):
            raise RelationViolationError(f"{record.__name__} contains old and new claim columns")
        return changed

    @classmethod
    def rename_record(cls, record: type, raw: dict[str, object]) -> dict[str, object]:
        result = raw
        for entry in FieldCodec.field_plans(record):
            old = cls.retired_fields(record).get(entry.name)
            if old is not None and old in result:
                if entry.key in result:
                    raise RelationViolationError(
                        f"{record.__name__} contains both {old} and {entry.key}")
                result = {entry.key if key == old else key: value
                          for key, value in result.items()}
        return result

    @classmethod
    def convert(cls, declared: object, raw: object) -> object:
        return compile_decoder(declared).rewrite_records(raw, cls.rename_record, cls.selects_record)
