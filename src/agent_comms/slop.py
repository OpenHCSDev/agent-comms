"""Slop citations: the refactor-audit pattern a restated fact matches, where it is
restated, the mechanism that derives it and the declaration that owns it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import ClassVar

from agent_comms import skills
from agent_comms.change_report import FileLine
from agent_comms.declared_family import DeclaredFamily


class Derivation(DeclaredFamily, affix="Derivation"):
    sentence: ClassVar[str]

    @classmethod
    def declaration_elsewhere(cls, by: FileLine, where: FileLine, root: Path) -> bool:
        return False


class DeclarationDerivation(Derivation):
    sentence = "the declaration derives it"

    @classmethod
    def declaration_elsewhere(cls, by: FileLine, where: FileLine, root: Path) -> bool:
        return not by.same_file(where, root)


class NominalKeyDerivation(Derivation, declared_name="nominal-key"):
    sentence = "the nominal key identifies it"


class SubclassDerivation(DeclarationDerivation):
    sentence = "the subclass derives it"


class MixinDerivation(DeclarationDerivation):
    sentence = "the mixed-in capability derives it"


class BaseMethodDerivation(DeclarationDerivation, declared_name="base-method"):
    sentence = "the base method derives it"


class InitSubclassDerivation(DeclarationDerivation, declared_name="init-subclass"):
    sentence = "subclass initialization derives it"


class MetaclassDerivation(DeclarationDerivation):
    sentence = "the metaclass derives it"


class DeleteCopyDerivation(Derivation, declared_name="delete-copy"):
    sentence = "deleting the copy leaves its original declaration"


@dataclass(frozen=True)
class Slop:
    pattern: str
    fact: str
    at: FileLine
    derive: type[Derivation]
    by: FileLine

    PATTERN_ENTRY: ClassVar[re.Pattern] = re.compile(r'^## ([A-Z]+-\d+)\s+\S', re.M)

    @staticmethod
    def catalog() -> Path:
        """The pinned refactor-audit pattern catalog that slop citations name."""
        return Path(str(files(skills) / 'refactor-audit' / 'references' / 'patterns'))

    @classmethod
    @cache
    def patterns(cls) -> frozenset[str]:
        return frozenset(found[1] for source in cls.catalog().glob('*.md')
                         for found in cls.PATTERN_ENTRY.finditer(source.read_text(encoding='utf-8')))

    def __post_init__(self):
        problems = []
        if self.pattern not in self.patterns():
            problems.append(f'Unknown refactor-audit pattern ID {self.pattern!r}; '
                            'fix: use an ID from the packaged pattern catalog.')
        if len(self.fact.split()) < 4:
            problems.append('fact: fix: describe the duplicated fact in at least four words')
        if problems:
            raise ValueError('\n'.join(problems))

    def render(self):
        return (f'{self.at.to_wire()} restates {self.pattern}: {self.fact}. '
                f'{self.by.to_wire()} owns it; {self.derive.sentence}, so the copy is deleted.')
