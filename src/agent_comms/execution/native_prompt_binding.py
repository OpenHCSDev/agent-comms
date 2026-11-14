"""The request Pi must receive for a reserved input.

A reserved input's row holds the digest of the request its prompt becomes
(``NativeRuntimeInput.expected_prompt_digest``), written with the reservation. The
execution record determines which messages belong to the input; the digest does not
grant permission to run. A match is confirmed only when the private session journal
holds the same user-message digest and the required execution evidence is recorded.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.execution.image_input import ImageInput
from agent_comms.execution.native_entries import (
    NO_EVIDENCE_READ,
    NativeEvidenceRead,
    NoEvidenceRead,
)
from agent_comms.execution.native_pi import read_tracked_input_commitment
from agent_comms.execution.native_runtime_input import NativeRuntimeInput
from agent_comms.nominal.field_codec import FieldCodec

if TYPE_CHECKING:
    from agent_comms.execution.turn_inbox import StreamingInputPolicy


def encode_request(
    text: str, *, streaming_behavior: type[StreamingInputPolicy] | None = None,
    images: tuple[ImageInput, ...] = (),
) -> str:
    """JSON-encode like the pinned native ``_claimNativeInput`` request."""
    return FieldCodec.compact(
        {
            "kind": "prompt",
            "text": text,
            "images": FieldCodec.encode(images) if images else None,
            "streamingBehavior": FieldCodec.encode(streaming_behavior),
            "expandPromptTemplates": True,
            "source": "rpc",
        }, ensure_ascii=False,
    )


def native_request_digest(
    text: str, *, streaming_behavior: type[StreamingInputPolicy] | None = None,
    images: tuple[ImageInput, ...] = (),
) -> str:
    """sha256 over the pinned native request envelope, not the bare text."""
    return hashlib.sha256(
        ("pi-input-request-v1\n" + encode_request(
            text, streaming_behavior=streaming_behavior, images=images,
        )).encode("utf-8")
    ).hexdigest()


def expected_prompt_matches_journal(
    session_file: Path, reserved: NativeRuntimeInput, *,
    evidence: NativeEvidenceRead | NoEvidenceRead = NO_EVIDENCE_READ,
) -> bool:
    """Join the journal's digest of the input to the digest its reservation expects.

    A mismatch or an uncommitted input is NOT equality. An unreadable or
    malformed journal is a defect, not inequality, and raises.
    """
    observed = read_tracked_input_commitment(session_file, reserved.input_id, evidence=evidence)
    return observed.matches(reserved.expected_prompt_digest)
