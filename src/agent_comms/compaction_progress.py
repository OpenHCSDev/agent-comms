"""Measured source progress shared by native observation and turn presentation."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CompactionSourceProgress:
    source_bytes_done: int = field(metadata={"wire_name": "sourceBytesDone"})
    source_bytes_total: int = field(metadata={"wire_name": "sourceBytesTotal"})
    summary_phase: str = field(metadata={"wire_name": "summaryPhase"})

    def __post_init__(self):
        if not 0 <= self.source_bytes_done <= self.source_bytes_total:
            raise ValueError("Invalid compaction source progress")

    @property
    def percentage(self) -> int | None:
        return self.source_bytes_done * 100 // self.source_bytes_total if self.source_bytes_total else None

    @property
    def label(self) -> str:
        return f"{self.percentage}% of input processed" if self.percentage is not None else ""
