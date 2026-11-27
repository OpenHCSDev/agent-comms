from __future__ import annotations

from agent_comms.storage.locked_store import DurableFile, PublishedSQLiteFile


class ExternalTranscriptFile(DurableFile, PublishedSQLiteFile):
    """Where the durable external transcript store lives under a bus root.
    ExternalTranscript inherits it; readers that need only the path import this,
    not transcript_sources."""

    __slots__ = ()

    filename = "external_transcripts.sqlite3"
