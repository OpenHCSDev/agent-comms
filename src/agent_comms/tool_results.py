"""Lossless native edit evidence and its shared ACP content projection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class ToolDiff:
    """A patch reported by the tool that performed the edit, never inferred later."""

    text: str
    format: Literal["unified", "numbered"] = "unified"

    @classmethod
    def from_result(cls, name: str, result: object, ok: bool) -> ToolDiff | None:
        if name != "edit" or not ok or not isinstance(result, Mapping):
            return None
        details = result.get("details")
        if not isinstance(details, Mapping):
            return None
        patch = details.get("patch")
        if isinstance(patch, str) and patch.strip():
            return cls(patch)
        # Older Pi versions persisted a numbered display diff, not a patch.
        diff = details.get("diff")
        if isinstance(diff, str) and diff.strip():
            return cls(diff, "numbered")
        return None


def tool_result_content(
    tool_call_id: str, output: str, diff: ToolDiff | None = None
) -> list[dict[str, Any]]:
    """Use ACP's embedded text resource for patches with original hunk positions.

    ACP's oldText/newText diff describes complete file versions. Native Pi
    reports a patch instead, so inventing those versions would misrepresent
    omitted context. text/x-diff preserves the actual tool result for any client.
    """
    content: list[dict[str, Any]] = []
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
