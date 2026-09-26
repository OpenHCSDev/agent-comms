#!/usr/bin/env python3
"""Apply only the reviewed bounded proof-journal startup headroom to a copied Pi package."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ORIGINAL_SHA256 = "d8184d801d79e167eedaf69b43fb8df614656a4030bf458e2da52486ab51cdae"
RECOVERY_SHA256 = "b8b3deeffad82771762808c435617d03f4701c3ac14a9620f5e313545a8d6875"
OLD = 'if (raw.length > 16 * 1024 * 1024 || (raw && !raw.endsWith("\\n"))) {'
NEW = 'if (raw.length > 128 * 1024 * 1024 || (raw && !raw.endsWith("\\n"))) {'


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main(package: Path) -> None:
    source = package / "dist/core/agent-session.js"
    original = source.read_bytes()
    if digest(original) != ORIGINAL_SHA256 or original.count(OLD.encode()) != 1:
        raise SystemExit("Refusing to patch: copied Pi source is not the reviewed pinned input build")
    updated = original.replace(OLD.encode(), NEW.encode(), 1)
    if digest(updated) != RECOVERY_SHA256:
        raise SystemExit("Refusing to patch: recovery bytes differ from reviewed artifact")
    source.write_bytes(updated)
    print(f"reviewed copied Pi proof bound ready: {source}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply-proof-headroom.py COPIED_PI_PACKAGE_DIRECTORY")
    main(Path(sys.argv[1]))
