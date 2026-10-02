"""Turn contributors derived from one certified authored-source read."""

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping

from .exporting import (AuthoredSourceScope, FullLimit, WireExportBoundary,
                        WireExportFormat, WireTranscriptExporter)
from .field_codec import FieldCodec
from .messages import Message
from .retained_task_facts import RetainedTaskFacts
from .input_attempt import StoredInput
from .turn_context import ContextSegment, OwnerProvenance, WireProvenance


@dataclass(frozen=True, kw_only=True)
class RetainedSegment(ContextSegment):
    retained: RetainedTaskFacts
    scope: AuthoredSourceScope
    boundary: WireExportBoundary

    @classmethod
    def capture(cls, retained: RetainedTaskFacts, source: OwnerProvenance,
                owner, registry, boundary: WireExportBoundary) -> "RetainedSegment":
        sources = {message.reference: message for fact in retained.facts
                   for message in fact.wire_sources()}
        wording_sources = tuple(message.task.original_wording_context_source(
            retained.original_text_source(message)) for message in sources.values())
        return cls(provenance=tuple(dict.fromkeys((source,
                       *(WireProvenance(ref) for ref in sources), *wording_sources))),
                   retained=retained, boundary=boundary,
                   scope=AuthoredSourceScope(tuple(message.reference for message in
                       retained.current_authored_sources(owner, registry))))

    def text(self) -> str:
        return self.retained.text

    def original_text_source(self, declaration: Message) -> Message | StoredInput:
        return self.retained.original_text_source(declaration)

    def inspection(self) -> dict[str, object]:
        """Authored constraints retain their exact source and narrower export scope."""
        return dict(kind=self.declared_name, text=self.text(),
                    provenance=FieldCodec.encode(self.provenance), input_supplied=False,
                    authored=dict(scope="authored declarations and their original pinned inputs only",
                                  sources=FieldCodec.encode(self.scope.sources),
                                  facts=FieldCodec.encode(self.retained)),
                    export_scope="current authored declarations only; never goal, unpinned input, native artifact or failure state")

    def export(self, destination: Path | str, *, overwrite: bool = False):
        """Publish this immutable read's selection through the original writer."""
        selected = sorted((source for fact in self.retained.facts
                           for source in fact.wire_sources()
                           if source.reference in self.scope.sources), key=lambda source: source.seq)
        return WireTranscriptExporter(format=RetainedFormat(self), scope=self.scope,
                                      limit=FullLimit(), boundary=self.boundary).export(
            selected, Path(destination).expanduser(), overwrite=overwrite)


@dataclass(frozen=True)
class RetainedFormat(WireExportFormat):
    """An instruction artifact renders wording from its certified original row."""

    segment: RetainedSegment

    def header(self, metadata: Mapping[str, object]) -> bytes:
        return ("# Authored retained context\n"
                f"# metadata: {self.json_record(metadata)}\n\n").encode()

    def row(self, message: Message, stored: Mapping[str, object]) -> bytes:
        original = self.segment.original_text_source(message)
        provenance = dict(declaration=FieldCodec.encode(message.reference),
                          **message.task.original_wording_provenance(original),
                          task=FieldCodec.encode(message.task))
        return (f"# source: {self.json_record(provenance)}\n"
                + message.task.original_wording(original) + "\n\n").encode()
