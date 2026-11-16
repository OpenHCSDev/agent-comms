"""Carry saved arrow rows from the Textual fork's arrow keys to formbridge's.

The arrow identities moved from textual.keys to formbridge.key_declarations, so a saved
keymap row naming textual.keys.UpKey or a sibling carries to the new name once. Only
installer input accepts the old names.
"""

from cutover.carry_toad_arrow_keys import PreviousArrow, PreviousInvocationId, ToadArrowKeys
from formbridge.key_declarations import DownKey, LeftKey, RightKey, UpKey


class TextualArrow(PreviousArrow, declared_name=None):
    """An arrow key the Textual fork declared before formbridge owned it."""


class TextualUpArrow(TextualArrow):
    previous = (PreviousInvocationId("textual.keys.UpKey"),)
    current = UpKey


class TextualDownArrow(TextualArrow):
    previous = (PreviousInvocationId("textual.keys.DownKey"),)
    current = DownKey


class TextualLeftArrow(TextualArrow):
    previous = (PreviousInvocationId("textual.keys.LeftKey"),)
    current = LeftKey


class TextualRightArrow(TextualArrow):
    previous = (PreviousInvocationId("textual.keys.RightKey"),)
    current = RightKey


class ToadFormbridgeArrowKeys(ToadArrowKeys):
    arrows = TextualArrow
