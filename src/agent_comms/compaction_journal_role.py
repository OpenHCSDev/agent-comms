"""Journal roles share the same database owner, never a separate connection policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .compaction_journal import CompactionJournal


@dataclass
class JournalRole:
    journal: CompactionJournal
