from __future__ import annotations
from dataclasses import dataclass
from dataclasses import InitVar
Alias = InitVar[int]
@dataclass
class Carrier:
    value: Alias
    def __post_init__(self, value):
        self.seen = value
