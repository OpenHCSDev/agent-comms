"""Transcript record ids spelled the way Claude Code writes them, one stable id per label."""

import uuid

from agent_comms.field_codec import FieldCodec, TranscriptRecordId

RECORDS = uuid.UUID("6f8e2a3c-1d4b-4c5a-9e7f-2b1c3d4e5f60")


def transcript_record(label: str) -> TranscriptRecordId:
    """The same label always names the same record, so an expected observation and the
    transcript line that produced it are spelled from one value."""
    return FieldCodec.decode(TranscriptRecordId, str(uuid.uuid5(RECORDS, label)))
