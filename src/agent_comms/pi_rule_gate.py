"""The Pi leaf of the rule gate (rule_deal): Core deals a Pi thread its rules when Pi reports a
finished compaction, and Core's coding tool owner judges every Pi tool call against that deal.

The deal lives in the thread's scratch directory, the one place its storage rule lets it write
outside its checkouts, so the audit it owes is a file its Write tool may create. A refusal is a
tool denial: Pi shows its text to the model as the tool's error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .rule_deal import DealtRuleGate
from .tool_denial import SelectedToolDenied

if TYPE_CHECKING:
    from .comms import Comms
    from .threads import Thread


class RulesUnread(SelectedToolDenied):
    """A Pi tool call refused until the thread has read its dealt rules and written its audit."""


@dataclass(frozen=True)
class PiRuleGate(DealtRuleGate):
    scratch: Path

    @classmethod
    def of(cls, comms: Comms, thread: Thread) -> PiRuleGate:
        return cls(comms.registry.storage_rule.roots_of(thread).scratch)

    @property
    def stem(self) -> Path:
        return self.scratch / "compaction-deal"

    def refuse(self, reason: str) -> None:
        raise RulesUnread(reason)
