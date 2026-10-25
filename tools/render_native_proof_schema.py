"""Render the committed native proof schema from NativeContextJournal."""

import json
import sys
from pathlib import Path

from agent_comms.native_pi import NativeContextJournal


def main() -> None:
    target = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(__file__).resolve().parents[1] / "stack/native/native-proof-schema.mjs"
    )
    target.write_text(
        "// Rendered at build from NativeContextJournal; never edited.\n"
        "export const nativeProofSchema = "
        + json.dumps(NativeContextJournal.native_contract(), indent=2)
        + ";\n"
    )


if __name__ == "__main__":
    main()
