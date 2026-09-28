"""Publish a prepared runtime after D22 installation; never stop or start clients.

Run using the new installed interpreter. The separate quiet-owner operator owns
restart. Delete this one-shot operator after the real conversion is complete.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from pathlib import Path

from agent_comms.active_route import (
    ActiveRoute,
    _publish_active_route_locked,
    active_route_path,
    read_active_route,
)
from agent_comms.cohort_foreground import _preflight
from agent_comms.comms import Comms
from agent_comms.store_files import _atomic_write_text


def activate(runtime: Path, package: Path, receipt: Path) -> None:
    runtime, package = runtime.absolute(), package.absolute()
    if Path(sys.executable).absolute().parent != runtime / "bin":
        raise ValueError("Run with the new installed runtime interpreter")
    if receipt.exists():
        raise ValueError("Use a new activation receipt; preserve prior outcomes")
    route = read_active_route()
    if route is None:
        raise ValueError("Expected the existing live route")
    comms = Comms(route.root)
    if any(thread.process_alive for thread in comms.registry.all_threads()):
        raise ValueError("Registered owners must be stopped before activation")
    replacement = ActiveRoute(route.root, route.wire_root_id, package)
    _preflight(replacement.root, replacement.wire_root_id, package, True)
    names = (
        "toad",
        "agent-comms",
        "agent-comms-acp",
        "agent-comms-agent",
        "agent-comms-nk-foreground",
    )
    links = {name: Path.home() / ".local/bin" / name for name in names}
    for name, link in links.items():
        if not link.is_symlink() or not os.access(runtime / "bin" / name, os.X_OK):
            raise ValueError(f"Unprepared launcher: {name}")
    record = {
        "root": str(route.root),
        "wire_root_id": route.wire_root_id,
        "previous_package": str(route.native_package),
        "package": str(package),
        "runtime": str(runtime),
        "previous_links": {name: os.readlink(link) for name, link in links.items()},
        "route_published": False,
        "links_published": [],
        "error": None,
    }
    _atomic_write_text(receipt, json.dumps(record, indent=2))
    directory = os.open(active_route_path().parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        _publish_active_route_locked(replacement, active_route_path(), directory, expected=route)
        record["route_published"] = True
        for name, link in links.items():
            temporary = link.with_name(f".{name}.d22-{os.getpid()}")
            temporary.symlink_to(runtime / "bin" / name)
            try:
                temporary.replace(link)
            finally:
                temporary.unlink(missing_ok=True)
            record["links_published"].append(name)
        launcher_directory = os.open(Path.home() / ".local/bin", os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(launcher_directory)
        finally:
            os.close(launcher_directory)
        if read_active_route() != replacement:
            raise ValueError("Active route differs after publication")
        for name, link in links.items():
            if link.resolve() != runtime / "bin" / name:
                raise ValueError(f"Launcher differs after publication: {name}")
    except BaseException as error:
        record["error"] = repr(error)
        raise
    finally:
        os.close(directory)
        _atomic_write_text(receipt, json.dumps(record, indent=2))
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("package", type=Path)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    activate(args.runtime, args.package, args.receipt)
