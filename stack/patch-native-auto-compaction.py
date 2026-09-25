#!/usr/bin/env python3
"""Restore threshold compaction before a new tracked prompt on the pinned Pi copy."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

BASE_SHA = "424066056076cbe2861c12277cb745428b13bf3308a8b9873d3064b62dc740d7"
OLD = "            if (lastAssistant && !options?.inputId) {\n"
NEW = (
    "            // A prior successful response may compact before this new tracked input.\n"
    "            // Failed tracked attempts still require an explicit recovery decision.\n"
    '            if (lastAssistant && (!options?.inputId || lastAssistant.stopReason === "stop")) {\n'
)


def main(path: Path) -> None:
    before = path.read_bytes()
    if hashlib.sha256(before).hexdigest() != BASE_SHA:
        raise SystemExit("Native session source does not match the pinned Pi build")
    source = before.decode()
    if source.count(OLD) != 1:
        raise SystemExit("Native pre-prompt compaction anchor changed")
    path.write_text(source.replace(OLD, NEW, 1))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
