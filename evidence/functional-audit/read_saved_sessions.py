"""Read bounded pages of preserved sessions through the installed parser.

All registry writes are confined to a disposable owned fixture; saved native
files are referenced for reads only. No owner, native process or input starts.
"""

import json
import tempfile
from pathlib import Path

from agent_comms import Thread, ThreadRole, wire


def main():
    root = Path("/var/tmp/agent-comms-live-20260927-wzjtqhza")
    checkout = Path(__file__).resolve().parents[2]
    sources = [
        root,
        *[Path(item["root"]) for item in json.loads((root / "history_sources.json").read_text())],
    ]
    sessions = {}
    for source in sources:
        for name, row in json.loads((source / "registry.json").read_text())["threads"].items():
            if path := row.get("session_file"):
                sessions.setdefault(path, []).append({"source": str(source), "name": name})
    report = []
    with tempfile.TemporaryDirectory(
        prefix="saved-read-", dir=checkout / ".audit-artifacts"
    ) as temp:
        comms = wire(Path(temp))
        for index, (path, refs) in enumerate(sessions.items()):
            name = f"audit-reader-{index}"
            comms.register(
                Thread(
                    name=name,
                    tags=frozenset(),
                    worktree=temp,
                    session_file=path,
                    role=ThreadRole.USER,
                    created_at=index + 1.0,
                )
            )
            row = {"path": path, "references": refs}
            try:
                page = comms.thread_transcript_page(name, max_messages=20, max_bytes=65536)
                row.update(
                    events=len(page.events),
                    has_older=page.has_older,
                    before=page.before.offset,
                    after=page.after.offset,
                )
                if page.has_older:
                    older = comms.thread_transcript_page(
                        name, before=page.before, max_messages=20, max_bytes=65536
                    )
                    assert older.before.offset < page.before.offset or not older.has_older
                    row["older_events"] = len(older.events)
                row["read_ok"] = True
            except Exception as error:
                row.update(read_ok=False, error=f"{type(error).__name__}: {error}")
            report.append(row)
        assert comms.bus.latest_sequence() == 0
    result = {
        "installed_bounded_parser": True,
        "sessions": len(report),
        "read_ok": sum(r["read_ok"] for r in report),
        "results": report,
        "no_inputs_sent": True,
    }
    (checkout / "evidence/functional-audit/saved-session-pages.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps({k: v for k, v in result.items() if k != "results"}))
    print("Empty pages:", [r["references"][0]["name"] for r in report if r.get("events") == 0])
    print("Failures:", [r for r in report if not r["read_ok"]])


if __name__ == "__main__":
    main()
