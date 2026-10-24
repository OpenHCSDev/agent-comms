"""The producer's item identity within its native turn."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .field_codec import NominalSpelling, NominalText


class NativeTurnId(NominalText):
    """A harness's own turn key: a Codex turn id, a Claude prompt id, an OpenCode message id
    or a Pi input id. Never one of our Id128 kinds; only equality is defined.
    """


class NativeTurnIdText(NominalSpelling[NativeTurnId]):
    spelling = re.compile(r"\S{1,4096}")


@dataclass(frozen=True)
class TurnItem:
    turn_id: NativeTurnId
    item_id: str

    @property
    def key(self) -> TurnItem:
        return TurnItem(self.turn_id, self.item_id)
