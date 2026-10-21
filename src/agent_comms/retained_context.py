"""Build a turn's saved context from one checked read of its authored records."""

from dataclasses import dataclass
from collections.abc import Mapping

from .exporting import WireExportFormat
from .field_codec import FieldCodec
from .messages import Message
from .context_segments.retained import RetainedSegment


@dataclass(frozen=True)
class RetainedFormat(WireExportFormat):
    """Write instruction text using the checked record that supplied its wording."""

    segment: RetainedSegment

    def header(self, metadata: Mapping[str, object]) -> bytes:
        return ("# Authored retained context\n"
                f"# metadata: {self.json_record(metadata)}\n\n").encode()

    def row(self, message: Message) -> bytes:
        original = self.segment.retained.original_text_source(message.reference)
        provenance = dict(declaration=FieldCodec.encode(message.reference),
                          **message.task.original_wording_provenance(original),
                          task=FieldCodec.encode(message.task))
        return (f"# source: {self.json_record(provenance)}\n"
                + message.task.original_wording(original) + "\n\n").encode()
