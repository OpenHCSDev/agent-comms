"""The ``agent-comms-resources`` CLI.

Three verbs, one per concrete consumer of this package:

* ``scan`` runs every :class:`ResourceLimit` member and prints the JSON summary
  the old ``agent_resource_check.py --assert-headroom`` printed.
* ``users PATH [PATH ...]`` emits a JSON line per process holding any handle
  on the given paths.
* ``free PATH [PATH ...]`` prints each given path no process holds; exits 1
  if any process was unreadable (we might be missing a holder).

The verdict dispatch follows :class:`ResourceLimit.members()`; a new resource
is one subclass under ``agent_comms.resources.limits``, not an edit here.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from agent_comms.resources.directory_queries import DirectoryQuery, ProcessPathWalk
from agent_comms.resources.limit import Act, ResourceLimit, Scan, scan
from agent_comms.resources.limits import (
    BranchLimit,
    CPULimit,
    HomeDiskLimit,
    MemoryLimit,
    MemoryStallLimit,
    RootDiskLimit,
    ShmDiskLimit,
    SwapLimit,
    TmpDiskLimit,
)

STATE_FILE = Path.home() / ".local/state/agent-comms/resource-alert.json"


def scan_limits(root: Path) -> tuple[ResourceLimit, ...]:
    """The limit set the old ``agent_resource_check`` watched, plus the two new
    members (CPU, branches). Discovery stays explicit here so the CLI's
    contract is readable; family consumers that want every declared member
    call :meth:`ResourceLimit.members` themselves."""
    return (
        RootDiskLimit(),
        TmpDiskLimit(),
        ShmDiskLimit(),
        HomeDiskLimit(root=root),
        MemoryLimit(),
        MemoryStallLimit(),
        SwapLimit(),
        CPULimit(),
        BranchLimit(),
    )


def scan_report(result: Scan) -> dict:
    """Shape the scan into JSON: each observation contributes its own summary fields."""
    free_gib: dict[str, float] = {}
    report: dict[str, object] = {
        "level": result.level,
        "reasons": list(result.reasons),
        "free_gib": free_gib,
        "ram_available_gib": None,
        "swap_used_gib": None,
        "verdicts": [
            {
                "resource": verdict.observation.resource,
                "severity": verdict.declared_name,
                "summary": verdict.observation.summary,
                "reason": verdict.report_reason,
            }
            for verdict in result.verdicts
        ],
        "failures": [{"resource": name, "error": error}
                     for name, error in result.failures],
    }
    for verdict in result.verdicts:
        verdict.observation.contribute_report(free_gib, report)
    return report


def monitor_notify(level: str, reasons: tuple[str, ...]) -> None:
    """Keep the old ``--monitor`` behaviour: notify-send on a level change."""
    try:
        previous = json.loads(STATE_FILE.read_text()).get("level")
    except (FileNotFoundError, PermissionError, ValueError):
        previous = None
    if previous == level:
        return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"level": level, "reasons": list(reasons)}) + "\n")
    if level != "healthy" and shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "-u", "critical" if level == "act" else "normal",
             "Agent resource pressure", "; ".join(reasons)],
            timeout=3, check=False,
        )


def _scan(args: argparse.Namespace) -> int:
    result = scan(scan_limits(args.root))
    print(json.dumps(scan_report(result), sort_keys=True))
    if args.monitor:
        monitor_notify(result.level, result.reasons)
    if args.assert_healthy and result.level != "healthy":
        return 2
    return 0


def _query(name: str, args: argparse.Namespace) -> int:
    query = DirectoryQuery.member_named(name)()
    walk = ProcessPathWalk(
        paths=tuple(args.paths),
        query_options=tuple(cls.option.encode()
                            for cls in DirectoryQuery.members_with(DirectoryQuery)),
    )
    return query.report(walk)


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-comms-resources", description=__doc__)
    subparsers = parser.add_subparsers(dest="verb", required=True)

    scan_parser = subparsers.add_parser("scan", help="run every resource limit and print JSON")
    scan_parser.add_argument(
        "--root", type=Path,
        default=HomeDiskLimit.default_root,
        help="project root for HomeDiskLimit's StorageRule lookup",
    )
    scan_parser.add_argument("--assert-healthy", action="store_true",
                             help="exit 2 if any limit is not healthy")
    scan_parser.add_argument("--monitor", action="store_true",
                             help="notify-send on a level change from the stored state")
    scan_parser.set_defaults(handler=lambda args: _scan(args))

    users_parser = subparsers.add_parser(
        "users", help="emit a JSON line per process holding handles on the paths")
    users_parser.add_argument("paths", nargs="+", type=Path)
    users_parser.set_defaults(handler=lambda args: _query("users", args))

    free_parser = subparsers.add_parser(
        "free", help="print each path no process holds; refuse on unreadable processes")
    free_parser.add_argument("paths", nargs="+", type=Path)
    free_parser.set_defaults(handler=lambda args: _query("free", args))

    args = parser.parse_args()
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()
