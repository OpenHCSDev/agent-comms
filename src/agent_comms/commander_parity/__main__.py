"""Render the commander matrix from its owning declarations."""

import argparse
import json
from pathlib import Path

from agent_comms.commander.config import CommanderConfig
from agent_comms.commander_parity import ParityRow
from agent_comms.launch_root import LaunchRoot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, help="Render the full plan instead of only the table")
    parser.add_argument(
        "--verify", action="store_true", help="Run live comparisons; do not grant cutover"
    )
    args = parser.parse_args()
    if args.verify:
        config = CommanderConfig.read(CommanderConfig.path())
        with LaunchRoot.from_path(config.root).selected():
            comparisons = tuple((row, row.verify()) for row in ParityRow.rows())
        print(
            json.dumps(
                {
                    "verified": sum(result.passed for _, result in comparisons),
                    "total": len(comparisons),
                    "rows": [
                        dict(
                            number=row.number,
                            capability=row.capability,
                            passed=result.passed,
                            result=result.describe(),
                        )
                        for row, result in comparisons
                    ],
                },
                indent=2,
            )
        )
        raise SystemExit(0 if all(result.passed for _, result in comparisons) else 1)
    print(ParityRow.render_plan(args.plan) if args.plan else ParityRow.matrix(), end="")


if __name__ == "__main__":
    main()
