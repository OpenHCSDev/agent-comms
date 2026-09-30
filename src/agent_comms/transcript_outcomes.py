"""Paging evidence for original compaction journal outcomes, not admission state."""

from dataclasses import dataclass, replace

from .thread_identity import ThreadIncarnation


@dataclass(frozen=True)
class CompactionOutcomeCursor:
    root: str
    recipient: ThreadIncarnation
    session_file: str
    revision: str
    sequence: int

    @classmethod
    def capture(cls, root, thread, session_file, registry):
        from .compaction_journal import CompactionJournal

        source = CompactionJournal.snapshot(
            root / "compaction-commits.sqlite3", session_file, thread.incarnation, registry,
        )
        cursor = (
            cls(str(root), thread.incarnation, session_file, source.revision.value,
                source.outcomes[-1].sequence)
            if source.outcomes else None
        )
        return source, cursor

    @property
    def identity(self):
        return self.root, self.recipient, self.session_file, self.revision

    def contains(self, other: CompactionOutcomeCursor) -> bool:
        return self.identity == other.identity and self.sequence >= other.sequence

    def at(self, sequence: int) -> CompactionOutcomeCursor:
        return replace(self, sequence=sequence)
