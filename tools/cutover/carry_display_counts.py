"""Discard schema 5 display indexes: read positions no longer belong in them."""
from pathlib import Path

from cutover.carry import Carried, Carry


class DisplayCounts(Carry):
    after = "carry_external_pages"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        paths = tuple(root.glob("bus_display_5_*.json"))
        if apply:
            for path in paths:
                path.unlink()
        return Carried(len(paths), rewritten=tuple(path.name for path in paths))


if __name__ == "__main__":
    DisplayCounts.main()
