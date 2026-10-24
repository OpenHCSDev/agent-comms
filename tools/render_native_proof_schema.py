"""Render the native proof writer's schema into a built tree from NativeContextJournal.

The build renders it from the checkout it builds; no copy is kept in the repository.
"""

import json
import sys
from pathlib import Path

from agent_comms.native_pi import NativeContextJournal


def main() -> None:
    Path(sys.argv[1]).write_text(
        "// Rendered at build from NativeContextJournal; never edited.\n"
        "export const nativeProofSchema = "
        + json.dumps(NativeContextJournal.native_contract(), indent=2)
        + ";\n"
    )


if __name__ == "__main__":
    main()
