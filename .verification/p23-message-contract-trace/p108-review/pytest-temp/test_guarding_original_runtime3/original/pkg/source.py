from __future__ import annotations
from dataclasses import dataclass
from typing import ClassVar
Alias = ClassVar[int]
@dataclass
class Carrier:
    value: Alias = 35
