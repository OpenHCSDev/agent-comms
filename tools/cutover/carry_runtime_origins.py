"""Carry flat runtime rows without inventing births or field-publication clocks.

The retired input exists only in this installer step. RuntimeInfoStore accepts
only its versioned successor. Delete this decoder after the carry is retired.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, ClassVar

from pydantic import JsonValue

from agent_comms.agent_events import ContextInfo, ModelObservation
from agent_comms.errors import RelationViolationError
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.nominal.field_codec import FieldCodec, SingleFieldValue, WireName, WireRequired
from agent_comms.runtime_field import UnattributedRuntimeOrigin
from agent_comms.runtime_info import AgentRuntimeInfo, RuntimeInfoDocument, RuntimeInfoStore
from agent_comms.thread_identity import UnrecordedThreadIncarnation


@dataclass(frozen=True)
class PreviousRuntimeInfo(ContextInfo, ModelObservation):
    """The stored snapshot shares its three value declarations, not their publication claim."""

    thread: str
    timestamp: Annotated[float, WireName("ts"), WireRequired()] = field(kw_only=True)

    def __post_init__(self) -> None:
        UnrecordedThreadIncarnation(self.thread)
        FieldCodec.decode(float, self.timestamp)

    def carried(self, root_name: str) -> AgentRuntimeInfo:
        origin = UnattributedRuntimeOrigin(root_name, self.thread, self.timestamp)
        return self.project_runtime_info(AgentRuntimeInfo(), origin)


@dataclass(frozen=True)
class PreviousRuntimeInfoDocument(SingleFieldValue):
    """The previous flat map; a row spelling need not equal its original map key."""

    observations: dict[str, PreviousRuntimeInfo] = field(default_factory=dict)

    @staticmethod
    def needed(document: dict[str, JsonValue]) -> bool:
        marker = FieldCodec.record_key(RuntimeInfoDocument, "version")
        # An old thread may literally be named "version". Its value is a row
        # object, not a format marker. Other present markers must validate as
        # current; malformed/future formats never fall back to old decoding.
        return marker not in document or isinstance(document[marker], dict)

    def carried(self) -> RuntimeInfoDocument:
        return RuntimeInfoDocument({
            UnrecordedThreadIncarnation(name): row.carried(name)
            for name, row in self.observations.items()
        })


class RuntimeOrigins(Carry):
    """Only the existing runtime-info writer publishes a converted document."""

    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RuntimeInfoStore,)

    def carry(self, *, apply: bool) -> Carried:
        changed = rows = current = 0
        rewritten = []
        for path in self.input_files():
            with RuntimeInfoStore.write_guard(path):
                document = FieldCodec.decode(dict[str, JsonValue], FieldCodec.parse(path.read_text()))
                if not PreviousRuntimeInfoDocument.needed(document):
                    FieldCodec.decode_document(RuntimeInfoDocument, document)
                    current += 1
                    continue
                previous = FieldCodec.decode_document(PreviousRuntimeInfoDocument, document)
                successor = previous.carried()
                # Compile and validate the complete successor before any write.
                encoded = FieldCodec.encode_document(successor, RuntimeInfoDocument)
                if FieldCodec.decode_document(RuntimeInfoDocument, encoded) != successor:
                    raise RelationViolationError(f"Runtime carry did not preserve every field: {path}")
                changed += 1
                rows += len(previous.observations)
                if apply:
                    # StoreLockAcquisition reuses this execution's held lock;
                    # replace keeps the store's rollback/fsync publication.
                    RuntimeInfoStore(path).replace(successor)
                    rewritten.append(path.relative_to(self.root))
        return Carried(changed, detail={"rows": rows, "current": current}, rewritten=tuple(rewritten))
