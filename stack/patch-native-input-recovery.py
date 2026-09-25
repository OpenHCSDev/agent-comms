#!/usr/bin/env python3
"""Validate startup proof rows against one derived snapshot of canonical entries."""

import hashlib
import sys
from pathlib import Path

SOURCE_SHA = "09f9bc9b6791532a307d115a868cf1e5552c53efaac48225aac03a3acd3d4901"


def main(session: Path) -> None:
    if hashlib.sha256(session.read_bytes()).hexdigest() != SOURCE_SHA:
        raise SystemExit("Native input recovery source does not match pinned build")
    source = session.read_text()
    replacements = (
        (
            "    _loadNativeInputState() {\n",
            "    _loadNativeInputState() {\n"
            "        // This startup-only index is derived from the same validated entries.\n"
            "        // Re-scanning the whole session for every journal row is quadratic.\n"
            "        const trackedEntries = new Map();\n",
        ),
        (
            "            this._nativeInputClaims.set(inputId, inputDigest);\n",
            "            this._nativeInputClaims.set(inputId, inputDigest);\n"
            "            trackedEntries.set(inputId, entry);\n",
        ),
        (
            "            const entry = this.sessionManager.getTrackedInput(row.inputId);\n",
            "            const entry = trackedEntries.get(row.inputId);\n",
        ),
    )
    for anchor, replacement in replacements:
        if source.count(anchor) != 1:
            raise SystemExit(f"Native input recovery anchor changed: {anchor!r}")
        source = source.replace(anchor, replacement, 1)
    session.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
