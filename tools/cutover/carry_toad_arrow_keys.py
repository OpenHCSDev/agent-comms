"""Carry retired per-control arrow identities into the native navigation declarations.

Only installer input accepts these names. The current settings decoder and runtime
key routing do not retain aliases for them.
"""

import re
from dataclasses import dataclass
from typing import ClassVar

from pydantic import JsonValue
from textual.keys import DownKey, LeftKey, NavigationKey, RightKey, UpKey

from agent_comms.declared_family import DeclaredFamily
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, NominalSpelling, NominalText
from cutover.carry_toad_key_mode import ToadSettingsCarry
from toad.key_modes import KeyMode
from toad.keymap import KeyInvocationId, Keymap, KeyRemap
from toad.preferences import ToadSettings, UiSettings


class PreviousInvocationId(NominalText):
    """A foreign saved identity, not yet admitted by the current catalog."""


class PreviousInvocationSpelling(NominalSpelling[PreviousInvocationId]):
    spelling = re.compile(r".+")


class PreviousArrow(DeclaredFamily, affix="Arrow"):
    previous: ClassVar[tuple[PreviousInvocationId, ...]]
    current: ClassVar[type[NavigationKey]]

    @classmethod
    def replacements(cls) -> dict[PreviousInvocationId, KeyInvocationId]:
        return {previous: KeyInvocationId(member.current().key_identity())
                for member in cls.members_with(cls) for previous in member.previous}


class PreviousUpArrow(PreviousArrow):
    previous = (PreviousInvocationId("textual.widgets._text_area.TextArea.action_cursor_up"),
                PreviousInvocationId("toad.widget_actions.CursorUpAction"))
    current = UpKey


class PreviousDownArrow(PreviousArrow):
    previous = (PreviousInvocationId("textual.widgets._text_area.TextArea.action_cursor_down"),
                PreviousInvocationId("toad.widget_actions.CursorDownAction"))
    current = DownKey


class PreviousLeftArrow(PreviousArrow):
    previous = (PreviousInvocationId("textual.widgets._text_area.TextArea.action_cursor_left"),
                PreviousInvocationId("textual.widgets._input.Input.action_cursor_left"),
                PreviousInvocationId("toad.widget_actions.CursorLeftAction"))
    current = LeftKey


class PreviousRightArrow(PreviousArrow):
    previous = (PreviousInvocationId("textual.widgets._text_area.TextArea.action_cursor_right"),
                PreviousInvocationId("textual.widgets._input.Input.action_cursor_right"),
                PreviousInvocationId("toad.widget_actions.CursorRightAction"))
    current = RightKey


@dataclass(frozen=True)
class PreviousKeyRemap:
    declaration: PreviousInvocationId
    mode: type[KeyMode]
    keys: tuple[str, ...]

    def carried(self, replacements: dict[PreviousInvocationId, KeyInvocationId]) -> KeyRemap:
        reference = replacements.get(self.declaration, KeyInvocationId(str(self.declaration)))
        return KeyRemap(reference, self.mode, self.keys)


class ToadArrowKeys(ToadSettingsCarry):
    def rewrite(self, document: dict[str, JsonValue]) -> int:
        ui_key = FieldCodec.record_key(ToadSettings, "ui")
        ui = FieldCodec.decode(dict[str, JsonValue], document.get(ui_key, {}))
        keymap_key = FieldCodec.record_key(UiSettings, "keymap")
        keymap = FieldCodec.decode(dict[str, JsonValue], ui.get(keymap_key, {}))
        bindings_key = FieldCodec.record_key(Keymap, "bindings")
        previous = FieldCodec.decode(tuple[PreviousKeyRemap, ...], keymap.get(bindings_key, []))
        replacements = PreviousArrow.replacements()
        changed = sum(row.declaration in replacements for row in previous)
        if not changed:
            return 0
        destinations = frozenset(replacements.values())
        arrows: dict[tuple[KeyInvocationId, type[KeyMode]], KeyRemap] = {}
        carried: list[KeyRemap] = []
        for row in previous:
            candidate = row.carried(replacements)
            if candidate.declaration in destinations:
                identity = candidate.declaration, candidate.mode
                existing = arrows.get(identity)
                if existing is not None:
                    if existing.keys != candidate.keys:
                        raise RelationViolationError(
                            f"Conflicting saved arrow choices for {candidate.declaration} "
                            f"in {candidate.mode.declared_name}: {existing.keys!r} vs {candidate.keys!r}"
                        )
                    continue
                arrows[identity] = candidate
            carried.append(candidate)
        keymap[bindings_key] = FieldCodec.encode(tuple(carried), tuple[KeyRemap, ...])
        ui[keymap_key] = keymap
        document[ui_key] = ui
        return changed
