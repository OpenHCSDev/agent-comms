#!/usr/bin/env python3
"""Give Pi's compaction summary the thread's task brief (PI_TASK) as custom instructions.

Manual and automatic compaction both pass through _runDefaultCompaction; the
brief precedes any instructions the /compact caller typed.
"""

import sys
from pathlib import Path

CALL = (
    "        return compact(preparation, requestModel, apiKey, headers, customInstructions, signal, "
)
BRIEF = (
    "        customInstructions = [process.env.PI_TASK, customInstructions]"
    '.filter(Boolean).join("\\n\\n") || undefined;\n'
)


def main(session: Path) -> None:
    source = session.read_text()
    if source.count(CALL) != 1:
        raise SystemExit("Native compaction summary call changed")
    session.write_text(source.replace(CALL, BRIEF + CALL, 1))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
