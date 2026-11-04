"""Preserve old name-only pin preferences without assigning them today's births."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from pydantic import JsonValue

from agent_comms.catalog_document import (
    CatalogDeclarations, CatalogDocument, ChannelOptions, ChannelPreferences,
)
from agent_comms.catalog_store import ChannelCatalog
from agent_comms.errors import RelationViolationError
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.thread_pins import ThreadPin


@dataclass(frozen=True, slots=True)
class PreviousChannelPreferences(ChannelOptions):
    """Only the changed old field differs from the shared preference declaration."""

    pinned_threads: frozenset[str] = frozenset()

    def carried(self) -> ChannelPreferences:
        values = {plan.name: plan.current(self)
                  for plan in FieldCodec.field_plans(ChannelOptions) if plan.declared.init}
        values[FieldCodec.field_plan(ChannelPreferences, "pinned_threads").name] = frozenset(
            ThreadPin.unattributed(name) for name in self.pinned_threads)
        return FieldCodec.construct(ChannelPreferences, values)


@dataclass(slots=True)
class PreviousCatalogDocument(CatalogDeclarations[PreviousChannelPreferences]):
    """The previous document has no version marker or recorded pin owners."""

    @staticmethod
    def needed(document: dict[str, JsonValue]) -> bool:
        return FieldCodec.record_key(CatalogDocument, "version") not in document

    def carried(self) -> CatalogDocument:
        values = {plan.name: plan.current(self)
                  for plan in FieldCodec.field_plans(CatalogDeclarations[PreviousChannelPreferences])
                  if plan.declared.init}
        values[FieldCodec.field_plan(CatalogDocument, "preferences").name] = {
            name: preference.carried() for name, preference in self.preferences.items()
        }
        return FieldCodec.construct(CatalogDocument, values)


class CatalogPinOrigins(Carry):
    """The catalog's existing writer owns publication and its maintenance/lock lifetime."""

    opened_files: ClassVar[tuple[type[RootFile], ...]] = (ChannelCatalog,)

    def carry(self, *, apply: bool) -> Carried:
        changed = pins = current = 0
        rewritten = []
        for path in self.input_files():
            with ChannelCatalog.write_guard(path):
                document = FieldCodec.decode(dict[str, JsonValue], FieldCodec.parse(path.read_text()))
                if not PreviousCatalogDocument.needed(document):
                    FieldCodec.decode_document(CatalogDocument, document)
                    current += 1
                    continue
                previous = FieldCodec.decode_document(PreviousCatalogDocument, document)
                successor = previous.carried()
                encoded = FieldCodec.encode_document(successor, CatalogDocument)
                if FieldCodec.decode_document(CatalogDocument, encoded) != successor:
                    raise RelationViolationError(f"Catalog carry did not preserve every field: {path}")
                changed += 1
                pins += sum(len(preference.pinned_threads) for preference in previous.preferences.values())
                if apply:
                    ChannelCatalog(path).replace(successor)
                    rewritten.append(path.relative_to(self.root))
        return Carried(changed, detail={"pins": pins, "current": current}, rewritten=tuple(rewritten))
