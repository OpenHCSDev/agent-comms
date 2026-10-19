"""The default route without a native package.

A version 1 route named the native package an installation launched; each
installation now launches its own pinned package, so the field goes and the
route becomes version 2. Only a route that selects this bus is rewritten.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from agent_comms.active_route import acquire_exclusive_route_lock, active_route_path, read_active_route

from .carry import Carried, Carry


def selects(path: Path, root: Path) -> bool:
    """A version 1 route to this bus."""
    if not path.exists():
        return False
    record = json.loads(path.read_text())
    return record.get("version") == 1 and Path(record["root"]).resolve() == root.resolve()


class RoutePackage(Carry):
    """The route keeps its root and root ID and drops the package."""

    after = 'carry_registry_format'

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = active_route_path()
        if not selects(path, root):
            return Carried(0)  # Another bus's route, read without waiting for its readers.
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            acquire_exclusive_route_lock(directory)
            if not selects(path, root):
                return Carried(0)
            record = json.loads(path.read_text())
            carried = {"version": 2, "root": record["root"], "wire_root_id": record["wire_root_id"]}
            if apply:
                descriptor, stage = tempfile.mkstemp(dir=path.parent, prefix=".active-route.")
                try:
                    os.fchmod(descriptor, 0o600)
                    os.write(descriptor, (json.dumps(carried, sort_keys=True) + "\n").encode())
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.replace(stage, path)
                os.fsync(directory)
                read_active_route(path)
        finally:
            os.close(directory)
        return Carried(1)


if __name__ == "__main__":
    RoutePackage.main()
