"""Behaviour failure patterns Core catches: each declared once with its signature and check.

A member names what the pattern looks like in state, readable without trusting
a report, the existing check that enforces it, and where the pattern was learned.
``docs/agents/behaviour-catalog.md`` is generated from these declarations.
"""

from __future__ import annotations

from typing import ClassVar

from .declared_family import DeclaredFamily


class BehaviourPattern(DeclaredFamily, affix="Pattern"):
    signature: ClassVar[str]
    source: ClassVar[str]
