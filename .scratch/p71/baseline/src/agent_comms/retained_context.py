"""Turn contributors derived from one certified authored-source read."""

from dataclasses import dataclass
from collections.abc import Mapping

from .exporting import WireExportFormat
from .field_codec import FieldCodec
from .messages import Message
from .context_segments.retained import RetainedSegment


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
