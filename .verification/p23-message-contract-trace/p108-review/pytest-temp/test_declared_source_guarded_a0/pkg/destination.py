from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pkg.shared import Alias


@dataclass
class Carrier:
    value: Alias
