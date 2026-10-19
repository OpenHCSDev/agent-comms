"""The pinned refactor-audit pattern catalog, the one owner of which antipatterns exist and
which fact kind each restates; agent-comms reads both from it and declares neither."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from .package_resources import resource


@dataclass(frozen=True)
class FactKind:
    """A kind of required fact slop restates, as the catalog's Fact kinds section declares it."""

    name: str
    question: str


@dataclass(frozen=True)
class Pattern:
    id: str
    title: str
    kind: FactKind


@dataclass(frozen=True)
class PatternCatalog:
    KIND = re.compile(r"^- \*\*(\w+):\*\* (.+)$", re.M)
    ENTRY = re.compile(r"^## ([A-Z]+-\d+) (.+)\n(?:\n\*\*Restates:\*\* (\w+)$)?", re.M)

    root: Path
    patterns: dict[str, Pattern]

    @classmethod
    @cache
    def pinned(cls) -> PatternCatalog:
        """The catalog this checkout pins under external/, or the copy its wheel carries."""
        return cls.read(resource("external/refactor-audit/references/patterns", "_catalog"))

    @classmethod
    def read(cls, root: Path) -> PatternCatalog:
        section = (root / "README.md").read_text().split("\n## Fact kinds\n", 1)[1]
        kinds = {
            found[1]: FactKind(found[1], found[2])
            for found in cls.KIND.finditer(section.split("\n## ", 1)[0])
        }
        patterns = {}
        for path in sorted(root.glob("*.md")):
            for found in cls.ENTRY.finditer(path.read_text()):
                if found[3] not in kinds:
                    raise ValueError(
                        f"{path}: {found[1]} declares no fact kind of {root / 'README.md'}"
                    )
                patterns[found[1]] = Pattern(found[1], found[2], kinds[found[3]])
        return cls(root, patterns)

    def pattern(self, cited: str) -> Pattern:
        try:
            return self.patterns[cited]
        except KeyError:
            raise ValueError(f"{cited!r} is no pattern of the pinned catalog {self.root}") from None
