"""Render the commander matrix from its owning declarations."""

import argparse
from pathlib import Path

from . import ParityRow


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, help="Render the full plan instead of only the table")
    args = parser.parse_args()
    print(ParityRow.render_plan(args.plan) if args.plan else ParityRow.matrix(), end="")


if __name__ == "__main__":
    main()
