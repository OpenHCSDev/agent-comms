"""Lossless native edit evidence and its shared ACP content projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from .transcript_events import SentTranscript

SENT_MESSAGE_MIME = "application/vnd.agent-comms.sent-message+json"


@dataclass(frozen=True, slots=True)
class ToolText:
    """Plain tool output, shared by saved presentation and ACP encoding."""

    text: str

    def tool_content(self, tool_call_id):
        return {"type": "content", "content": {"type": "text", "text": self.text}}


@dataclass(frozen=True, slots=True)
class ToolDiff:
    """A patch reported by the tool that performed the edit, never inferred later."""

    text: str
    format: Literal["unified", "numbered"] = "unified"

    @property
    def mime_type(self) -> str:
        """Keep the producer's declared grammar across the ACP boundary."""
        return "text/x-diff" if self.format == "unified" else "application/vnd.agent-comms.numbered-diff"

    def tool_content(self, tool_call_id):
        return {"type": "content", "content": {
            "type": "resource", "resource": {
                "uri": f"agent-comms:///tool-diffs/{quote(tool_call_id, safe='')}",
                "mimeType": self.mime_type,
                "text": self.text,
            },
        }}

def tool_result_content(
    tool_call_id: str, output: str, diff: ToolDiff | None = None,
    sent_message: SentTranscript | None = None,
) -> list[dict[str, Any]]:
    """Use ACP's embedded text resource for patches with original hunk positions.

    ACP's oldText/newText diff describes complete file versions. Native Pi
    reports a patch instead, so inventing those versions would misrepresent
    omitted context. text/x-diff preserves the actual tool result for any client.
    """
    from .transcript_events import ToolEndTranscript

    return [
        part.tool_content(tool_call_id)
        for part in ToolEndTranscript.parts_for(output, diff, sent_message)
    ]
