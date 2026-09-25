"""Read-only bus-to-native-session timing evidence; never sends or acknowledges.

Select recipients explicitly: today's tags cannot prove historical membership.
Content matches are evidence, not provider receipts. Repeated identical sends,
quoted prompts, old session branches, or a changed session file can make them
ambiguous. Missing matches must not be interpreted as permission to replay.
"""

from __future__ import annotations

import argparse
import json
from collections import deque
from datetime import datetime
from pathlib import Path

from agent_comms.declarations import Message, ScheduledTurn


def timestamp_seconds(value: str | int | float) -> float:
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return value / 1000  # Pi message timestamps are epoch milliseconds.


def trace(root: Path, channel: str, recipients: list[str], after: int, last: int) -> dict:
    registry = json.loads((root / "registry.json").read_text())
    selected: deque[Message] = deque(maxlen=last)
    with (root / "bus.jsonl").open() as source:
        for line in source:
            row = json.loads(line)
            if row["to"] == channel and row["seq"] > after:
                selected.append(Message.from_wire(row))
    messages = list(selected)
    prompts = {message.seq: ScheduledTurn.incoming(message).prompt for message in messages}
    results = []
    cursors_path = root / "acp_delivery_cursors.json"
    cursors = json.loads(cursors_path.read_text()).get("rows", {}) if cursors_path.exists() else {}
    for name in recipients:
        thread = registry["threads"][name]
        session_file = thread.get("session_file")
        matches: dict[int, list[dict]] = {message.seq: [] for message in messages}
        error = None
        try:
            if not session_file:
                raise ValueError("No current native session file")
            with Path(session_file).open() as session:
                for line_number, line in enumerate(session, 1):
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        error = f"Malformed/incomplete session row {line_number}; scan incomplete"
                        break
                    native_message = entry.get("message", {})
                    if native_message.get("role") != "user":
                        continue
                    content = native_message.get("content", [])
                    text = (
                        content
                        if isinstance(content, str)
                        else "\n".join(
                            block.get("text", "") for block in content if isinstance(block, dict)
                        )
                    )
                    timestamp = entry.get("timestamp", native_message.get("timestamp"))
                    found = [message for message in messages if prompts[message.seq] in text]
                    for message in found:
                        identical = [
                            other.seq
                            for other in found
                            if prompts[other.seq] == prompts[message.seq]
                        ]
                        matches[message.seq].append(
                            {
                                "entry_id": entry.get("id"),
                                "line": line_number,
                                "timestamp": timestamp,
                                "publication_to_entry_seconds": (
                                    round(timestamp_seconds(timestamp) - message.timestamp, 3)
                                    if timestamp is not None
                                    else None
                                ),
                                "indistinguishable_sequences": (
                                    identical if len(identical) > 1 else []
                                ),
                            }
                        )
        except (OSError, ValueError) as exc:
            error = str(exc)
        results.append(
            {
                "recipient": name,
                "current_session_file": session_file,
                "current_delivery_cursor": cursors.get(name, {}).get("cursor"),
                "scan_error": error,
                "messages": [
                    {
                        "sequence": message.seq,
                        "sender": message.sender,
                        "published_at": message.timestamp,
                        "response_policy": message.response_policy.value,
                        "starts_turn_for_current_name": message.starts_turn_for(name),
                        "native_content_matches": matches[message.seq],
                    }
                    for message in messages
                ],
            }
        )
    return {
        "channel": channel,
        "read_only": True,
        "limitations": [
            "Current session only; historical membership and session switches are not reconstructed.",
            "Native content matches do not prove model consumption, successful reply, or UI paint.",
            "Repeated identical prompts cannot identify which publication caused an entry.",
            "Delivery cursors are observations, not successful-input receipts.",
            "Files are read independently and may change during this scan.",
        ],
        "recipients": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--recipient", action="append", required=True)
    parser.add_argument("--after-seq", type=int, default=0)
    parser.add_argument("--last", type=int, default=20)
    args = parser.parse_args()
    if args.last < 1:
        parser.error("--last must be positive")
    print(
        json.dumps(
            trace(args.root, args.channel, args.recipient, args.after_seq, args.last), indent=2
        )
    )


if __name__ == "__main__":
    main()
