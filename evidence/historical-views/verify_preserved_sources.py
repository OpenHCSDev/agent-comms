"""Read preserved roots into a new persistent fixture; never instantiate a live source."""

import argparse
import json
import shutil
import time
from pathlib import Path

from agent_comms import Comms, Message


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--active-root", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, action="append", required=True)
    args = parser.parse_args()
    root = args.fixture_root.resolve()
    if root.exists():
        raise ValueError("Use a new fixture directory, not an existing root")
    root.mkdir(mode=0o700, parents=True)
    for name in ("registry.json", "bus.jsonl", "channels.json", "channel_metadata.json"):
        path = args.active_root / name
        if path.exists():
            shutil.copyfile(path, root / name)
    comms = Comms(root)
    comms.user_identity(str(root))
    results = []
    for path in args.source:
        start = time.monotonic()
        source = comms.attach_history(path)
        with (path / "bus.jsonl").open() as stream:
            raw = [json.loads(line) for line in stream if line.strip()]
        assert all(
            row.get("id", Message.from_wire(row).message_id) == Message.from_wire(row).message_id
            for row in raw
        )
        results.append(
            {
                "source": str(path),
                "identity_count": len(source.registry().snapshot().threads),
                "messages": len(raw),
                "bytes": source.size,
                "attach_seconds": round(time.monotonic() - start, 3),
            }
        )
    for channel in ("#any", "#comms", "#nra", "#openhcs"):
        page = comms.channel_display_page(channel, worktree=str(root), limit=200)
        count, pages = len(page.messages), 1
        keys = {message.view_key for message in page.messages}
        start = time.monotonic()
        while page.has_older:
            page = comms.channel_display_page(
                channel, worktree=str(root), before=page.oldest_cursor, limit=200
            )
            count += len(page.messages)
            keys.update(message.view_key for message in page.messages)
            pages += 1
        assert len(keys) == count
        results.append(
            {
                "channel": channel,
                "messages": count,
                "pages": pages,
                "read_seconds": round(time.monotonic() - start, 3),
            }
        )
    sessions = []
    for item in comms.historical_threads():
        if not item.thread.session_file:
            continue
        path = Path(item.thread.session_file)
        if not path.is_file():
            sessions.append({"name": item.thread.name, "state": "missing"})
            continue
        try:
            page = comms.thread_transcript_page(
                item.thread.name, historical_source=item.source.key, max_messages=2
            )
        except (OSError, ValueError) as error:
            sessions.append({"name": item.thread.name, "state": "error", "error": str(error)})
        else:
            sessions.append(
                {"name": item.thread.name, "state": "readable", "events": len(page.events)}
            )
    results.append({"saved_sessions": sessions})
    (root / "validation.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
