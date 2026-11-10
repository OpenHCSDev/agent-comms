from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .source import Alias
@dataclass
class Carrier:
    value: Alias = 35
