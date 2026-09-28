"""Read-only /proc witness for an explicit old-root cold cutover.

This lists processes carrying AGENT_COMMS_ROOT for the selected root. It does
not prove that processes without that variable cannot write, and it never
stops a process or changes the wire. Re-run after the stop barrier.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _identity(pid: int) -> tuple[int, int] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except FileNotFoundError:
        return None
    end = raw.rfind(b") ")
    if end < 0:
        raise ValueError(f"PID {pid} has malformed proc status")
    fields = raw[end + 2 :].split()
    if len(fields) <= 19 or fields[0] == b"Z":
        return None
    return int(fields[1]), int(fields[19])


def inventory(root: Path) -> dict[str, object]:
    if not sys.platform.startswith("linux") or not root.is_absolute():
        raise ValueError("Cutover process inventory requires Linux and an absolute root")
    registry = json.loads((root / "registry.json").read_text())
    registered: dict[int, list[str]] = {}
    for name, thread in registry["threads"].items():
        pid = thread.get("pid")
        if type(pid) is int and pid > 0:
            registered.setdefault(pid, []).append(name)

    rows: list[dict[str, object]] = []
    unreadable: list[int] = []
    expected = b"AGENT_COMMS_ROOT=" + os.fsencode(root)
    for entry in sorted(Path("/proc").iterdir(), key=lambda item: item.name):
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.geteuid():
                continue
            before = _identity(pid)
            if before is None:
                continue
            environment = (entry / "environ").read_bytes().split(b"\0")
            after = _identity(pid)
            if after is None or after != before:
                continue
            if expected not in environment:
                continue
            rows.append({
                "pid": pid,
                "parent_pid": before[0],
                "start_ticks": before[1],
                "comm": (entry / "comm").read_text().strip(),
                "registry_names": sorted(registered.get(pid, ())),
            })
        except FileNotFoundError:
            continue  # The process exited during this snapshot.
        except (OSError, ValueError):
            unreadable.append(pid)
    return {
        "root": str(root),
        "scope": "exact explicit AGENT_COMMS_ROOT in readable owned processes",
        "processes": sorted(rows, key=lambda row: row["pid"]),
        "unreadable_owned_pids": sorted(set(unreadable)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / ".agent-comms")
    args = parser.parse_args()
    result = inventory(args.root.expanduser().resolve())
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
