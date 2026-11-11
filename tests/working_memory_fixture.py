"""Unattested local annotation data, never an SDK/provider observation."""
import hashlib

from agent_comms.execution.turn_context import (
    ContributionCoordinates,
    SystemLayerSegment,
    UnattributedProvenance,
)


def local_system(
    text: str, sources: tuple[ContributionCoordinates, ...] = (),
) -> SystemLayerSegment:
    raw = text.encode()
    return SystemLayerSegment(
        provenance=(UnattributedProvenance(),), content=text, tokens=0,
        sha256=hashlib.sha256(raw).hexdigest(), utf8_bytes=len(raw),
        source_spans=sources,
    )
