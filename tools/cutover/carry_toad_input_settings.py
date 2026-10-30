"""Carry the retired prompt preferences into Toad's input box settings group."""

from dataclasses import dataclass
from typing import Annotated

from pydantic import JsonValue

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, WireName
from cutover.carry_toad_key_mode import ToadSettingsCarry
from toad.preferences import InputSettings, ToadSettings, UiSettings
from toad.setting_choices import FlushInputBox, InputBox


@dataclass(frozen=True)
class PreviousCompactInput:
    """The retired flag only. True removed the prompt's spacing, which is now Flush."""

    compact_input: Annotated[bool, WireName("compact-input")]


@dataclass(frozen=True)
class PreviousPromptMessage:
    """The retired placeholder text, which no widget read; it is now the live placeholder."""

    prompt_message: str


class ToadInputSettings(ToadSettingsCarry):
    def rewrite(self, document: dict[str, JsonValue]) -> int:
        ui_key = FieldCodec.record_key(ToadSettings, "ui")
        ui = FieldCodec.decode(dict[str, JsonValue], document.get(ui_key, {}))
        compact_key = FieldCodec.record_key(PreviousCompactInput, "compact_input")
        message_key = FieldCodec.record_key(PreviousPromptMessage, "prompt_message")
        if compact_key not in ui and message_key not in ui:
            return 0
        input_key = FieldCodec.record_key(UiSettings, "input")
        if input_key in ui:
            raise RelationViolationError("Both old and current prompt preferences exist")
        carried: dict[str, JsonValue] = {}
        if compact_key in ui:
            previous = FieldCodec.decode(PreviousCompactInput, {compact_key: ui.pop(compact_key)})
            if previous.compact_input:
                carried[FieldCodec.record_key(InputSettings, "box")] = FieldCodec.encode(
                    FlushInputBox, type[InputBox]
                )
        if message_key in ui:
            previous_message = FieldCodec.decode(
                PreviousPromptMessage, {message_key: ui.pop(message_key)}
            )
            carried[FieldCodec.record_key(InputSettings, "placeholder")] = (
                previous_message.prompt_message
            )
        if carried:
            ui[input_key] = carried
        document[ui_key] = ui
        return 1
