"""Read the exact bus schema differences of an owned scratch carry result."""

import sys
from contextlib import closing
from pathlib import Path

from agent_comms.bus import Bus


def main() -> None:
    path = Path(sys.argv[1]) / Bus.filename
    schema = Bus.schema_at(path)
    with closing(Bus.read_snapshot(path)) as db:
        differences = schema.schema_differences(db)
        print(f"{path}: {len(differences)} declared schema differences")
        for difference in differences:
            print(difference)


if __name__ == "__main__":
    main()
