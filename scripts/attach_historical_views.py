"""Preview or attach preserved buses to normal Comms history (no execution replay)."""

import argparse
import json
from pathlib import Path

from agent_comms import Comms


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument(
        "--source",
        type=Path,
        action="append",
        required=True,
        help="Oldest source first. Each original root is attached once.",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    for source in args.source:
        raw = json.loads((source / "registry.json").read_text())
        bus = source / "bus.jsonl"
        print(
            json.dumps(
                {
                    "source": str(source.resolve()),
                    "identities": len(raw["threads"]),
                    "bus_bytes": bus.stat().st_size if bus.exists() else 0,
                }
            )
        )
    if args.apply:
        comms = Comms(args.destination)
        for source in args.source:
            attached = comms.views.attach_history(source)
            print(json.dumps({"attached": attached.key, "original_root": attached.original_root}))


if __name__ == "__main__":
    main()
