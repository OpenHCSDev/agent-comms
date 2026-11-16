"""Carry saved keymap rows naming the Textual fork's navigation commands to formbridge's.

The list and scroll commands moved from textual.navigation to
formbridge.navigation_commands, so a saved keymap row naming textual.navigation.PageUp or a
sibling carries to the new name once. Only installer input accepts the old names.
"""

from cutover.carry_toad_arrow_keys import PreviousArrow, PreviousInvocationId, ToadArrowKeys
from formbridge import navigation_commands
from formbridge.navigation_commands import NavigationCommand
from toad.keymap import KeyInvocationId


class TextualNavigationCommand(PreviousArrow, declared_name=None):
    """Every navigation command formbridge declares, under the name the fork gave it."""

    @classmethod
    def replacements(cls) -> dict[PreviousInvocationId, KeyInvocationId]:
        return {
            PreviousInvocationId(f"textual.navigation.{command.__qualname__}"):
                KeyInvocationId(command.identity())
            for command in NavigationCommand.declarations()
            if command.__module__ == navigation_commands.__name__
        }


class ToadNavigationCommandKeys(ToadArrowKeys):
    arrows = TextualNavigationCommand
