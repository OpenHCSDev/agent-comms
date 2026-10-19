"""Where agent_comms' resources live: in its source checkout, or inside its installed wheel."""

from pathlib import Path


def resource(checkout: str, packaged: str) -> Path:
    """``checkout`` under the checkout root when agent_comms runs from its src/ directory, even
    if build artifacts exist beside the source; otherwise the wheel's ``packaged`` directory."""
    package = Path(__file__).resolve().parent
    if package.name == "agent_comms" and package.parent.name == "src":
        return package.parents[1] / checkout
    return package / packaged
