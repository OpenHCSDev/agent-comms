"""Lossless native edit evidence and its shared ACP content projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from .transcript_events import SentTranscript

SENT_MESSAGE_MIME = "application/vnd.agent-comms.sent-message+json"


@dataclass(frozen=True, slots=True)
class ToolDiff:
    """A patch reported by the tool that performed the edit, never inferred later."""

    text: str
    format: Literal["unified", "numbered"] = "unified"

def tool_result_content(
    tool_call_id: str, output: str, diff: ToolDiff | None = None,
    sent_message: SentTranscript | None = None,
) -> list[dict[str, Any]]:
    """Use ACP's embedded text resource for patches with original hunk positions.

    ACP's oldText/newText diff describes complete file versions. Native Pi
    reports a patch instead, so inventing those versions would misrepresent
    omitted context. text/x-diff preserves the actual tool result for any client.
    """
    content: list[dict[str, Any]] = []
    if sent_message is not None:
        import json
        from .field_codec import FieldCodec

        return [{"type": "content", "content": {
            "type": "resource", "resource": {
                "uri": f"agent-comms:///messages/{sent_message.source.seq}/{quote(sent_message.source.message_id, safe='')}",
                "mimeType": SENT_MESSAGE_MIME,
                "text": json.dumps(FieldCodec.encode(sent_message)),
            },
        }}]
    if diff is not None:
        content.append(
            {
                "type": "content",
                "content": {
                    "type": "resource",
                    "resource": {
                        "uri": f"agent-comms:///tool-diffs/{quote(tool_call_id, safe='')}",
                        "mimeType": "text/x-diff",
                        "text": diff.text,
                    },
                },
            }
        )
    if output:
        content.append({"type": "content", "content": {"type": "text", "text": output}})
    return content
