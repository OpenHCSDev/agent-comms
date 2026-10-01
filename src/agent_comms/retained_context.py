"""Turn contributors derived from one certified authored-source read."""

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping

from .exporting import (AuthoredSourceScope, FullLimit, WireExportBoundary,
                        WireExportFormat, WireTranscriptExporter)
from .field_codec import FieldCodec
from .messages import Message
from .retained_task_facts import RetainedTaskFacts
from .turn_context import ContextSegment, OwnerProvenance, WireProvenance


@dataclass(frozen=True, kw_only=True)
class RetainedSegment(ContextSegment):
    retained: RetainedTaskFacts
    scope: AuthoredSourceScope
    boundary: WireExportBoundary

    @classmethod
    def capture(cls, retained: RetainedTaskFacts, source: OwnerProvenance,
                owner, registry, boundary: WireExportBoundary) -> "RetainedSegment":
        sources = dict.fromkeys(source.reference for fact in retained.facts
                                for source in fact.wire_sources())
        return cls(provenance=(source, *(WireProvenance(ref) for ref in sources)),
                   retained=retained, boundary=boundary,
                   scope=AuthoredSourceScope(tuple(message.reference for message in
                       retained.current_authored_sources(owner, registry))))

    def text(self) -> str:
        return self.retained.text

    def original_text_source(self, declaration: Message) -> Message:
        return self.retained.original_text_source(declaration)

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
                          wording=FieldCodec.encode(original.reference),
                          author=original.sender, author_role=original.sender_role.value,
                          task=FieldCodec.encode(message.task))
        return (f"# source: {self.json_record(provenance)}\n"
                + original.body + "\n\n").encode()
