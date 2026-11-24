"""Stdlib-only fast-path entry point for Antigravity lifecycle hooks (`hooks.json`).

Antigravity runs hooks synchronously on every invocation and tool call. When the session's
rule deal is already open and no `CHECKPOINT` compaction line was appended to `transcriptPath`
since the last check, this module answers immediately with standard-library I/O alone (~15 ms)
without importing Core. On startup, while a deal is open, or whenever `CHECKPOINT` appears in
the transcript tail, it hands off to `agent_comms.cli_bridges.antigravity_rule_gate`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path

ANTIGRAVITY_HOOK_NAME = "agent-comms-rule-gate"
_CHECKPOINT_MARKER = b'"CHECKPOINT"'
_FAST_EVENTS = frozenset({"PreInvocation", "PreToolUse", "PostToolUse"})


def hook_entry(command: str) -> dict[str, object]:
    """The named `hooks.json` entry Antigravity runs on start, invocation, and tools."""
    handler = {"type": "command", "command": command}
    return {
        "SessionStart": [handler],
        "PreInvocation": [handler],
        "PreToolUse": [{"matcher": "*", "hooks": [handler]}],
        "PostToolUse": [{"matcher": "*", "hooks": [handler]}],
    }


def _stem(payload: Mapping[str, object]) -> Path | None:
    transcript = payload.get("transcriptPath")
    if isinstance(transcript, str) and transcript:
        return Path(transcript).expanduser().with_suffix("")
    artifact = payload.get("artifactDirectoryPath")
    if isinstance(artifact, str) and artifact:
        return Path(artifact).expanduser() / "rule-deal"
    return None


def _fast_path(payload: Mapping[str, object]) -> dict[str, object] | None:
    event = payload.get("hookEventName")
    if not isinstance(event, str) or event not in _FAST_EVENTS:
        return None
    stem = _stem(payload)
    if stem is None:
        return None
    cursor_path = stem.with_suffix(".agy-checkpoint.json")
    deal_path = stem.with_suffix(".deal.json")
    try:
        cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        deal_mtime_ns = deal_path.stat().st_mtime_ns
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cursor, dict) or not cursor.get("gate_open"):
        return None
    if cursor.get("deal_mtime_ns") != deal_mtime_ns:
        return None
    transcript_raw = payload.get("transcriptPath")
    if isinstance(transcript_raw, str) and transcript_raw:
        transcript = Path(transcript_raw).expanduser()
        try:
            size = transcript.stat().st_size
            prev = int(cursor.get("transcript_size", 0))
            if size < prev:
                return None
            if size > prev:
                with transcript.open("rb") as fh:
                    fh.seek(prev)
                    if _CHECKPOINT_MARKER in fh.read(size - prev):
                        return None
                cursor["transcript_size"] = size
                cursor_path.write_text(json.dumps(cursor), encoding="utf-8")
        except (OSError, ValueError):
            return None
    return {"decision": "allow"} if event == "PreToolUse" else {}


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    answer = _fast_path(payload)
    if answer is None:
        from agent_comms.cli_bridges.antigravity_rule_gate import handle_hook

        answer = handle_hook(payload)
    sys.stdout.write(json.dumps(answer) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
