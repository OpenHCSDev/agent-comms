"""Stop old activity readers before any writer can publish the starting spelling.

Existing rows need no rewrite. Recording this step makes the first publication
use the carry chain's stopped-owner backup path instead of a gradual restart.
"""
from pathlib import Path

from .carry import Carried, Carry


class StartingActivity(Carry):
    after = "carry_external_positions"

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return ()

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        return Carried(0)


if __name__ == "__main__":
    StartingActivity.main()
