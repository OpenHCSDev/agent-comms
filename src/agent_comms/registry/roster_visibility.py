from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RosterVisibility:
    """Which threads a roster shows: the one declaration of its defaults."""

    stopped: bool = True
    archived: bool = False


DEFAULT_ROSTER = RosterVisibility()
