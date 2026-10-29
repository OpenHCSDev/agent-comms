"""Carry the saved Vim preference before the editable keymap replaces its wire field."""

from abc import abstractmethod
from dataclasses import dataclass
from typing import Annotated, ClassVar

from pydantic import JsonValue

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, WireName
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.store_files import AtomicFile
from toad.key_modes import KeyMode, Standard, VimNormal
from toad.preferences import SavedSettings, ToadSettings, UiSettings


@dataclass(frozen=True)
class PreviousVimPreference:
    """The retired input only. Current settings never accept this field."""

    vim_keys: Annotated[bool, WireName("vim-keys")]

    def mode(self) -> type[KeyMode]:
        # The old true value started Normal, rather than Insert or Visual.
        return VimNormal if self.vim_keys else Standard


class ToadSettingsCarry(Carry):
    """One confined saved-settings write, shared by its retired-input carries."""

    opened_files: ClassVar[tuple[type[RootFile], ...]] = ()
    writer_owned = (SavedSettings,)

    @abstractmethod
    def rewrite(self, document: dict[str, JsonValue]) -> int:
        """Rewrite this member's retired input in memory, or report no change."""

    def carry(self, *, apply: bool) -> Carried:
        resource, = SavedSettings.resources(self.files)
        path = self.files.location(resource)
        self.require_confined(path)
        if not path.exists():
            return Carried(0)
        document = FieldCodec.decode(dict[str, JsonValue], FieldCodec.parse(path.read_text()))
        changed = sum(member(self.files, self.spill_dir).rewrite(document)
                      for member in ToadSettingsCarry.members_with(ToadSettingsCarry))
        if not changed:
            return Carried(0)
        FieldCodec.decode(ToadSettings, document)
        if apply:
            AtomicFile(path).write_text(FieldCodec.compact(document, allow_nan=False))
        return Carried(changed, rewritten=(resource.path,) if apply else ())


class ToadKeyMode(ToadSettingsCarry):
    def rewrite(self, document: dict[str, JsonValue]) -> int:
        ui_key = FieldCodec.record_key(ToadSettings, "ui")
        ui = FieldCodec.decode(dict[str, JsonValue], document.get(ui_key, {}))
        previous_key = FieldCodec.record_key(PreviousVimPreference, "vim_keys")
        if previous_key not in ui:
            return 0
        mode_key = FieldCodec.record_key(UiSettings, "key_mode")
        if mode_key in ui:
            raise RelationViolationError("Both old and current keyboard preferences exist")
        previous = FieldCodec.decode(PreviousVimPreference, {previous_key: ui.pop(previous_key)})
        ui[mode_key] = FieldCodec.encode(previous.mode(), type[KeyMode])
        document[ui_key] = ui
        return 1
