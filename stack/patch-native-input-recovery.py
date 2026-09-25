#!/usr/bin/env python3
"""Validate startup proof rows against one derived snapshot of canonical entries."""

import hashlib
import sys
from pathlib import Path

SOURCE_SHA = "38a1ea134a702b63f00e1119fb5f459c147f577c23928d5ee59600659a06cb9a"


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
