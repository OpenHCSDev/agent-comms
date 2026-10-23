"""Families whose members name the SQL that coordinator rows format into their DDL."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from functools import cache
from typing import ClassVar

from .declared_family import DeclaredFamily
from .typed_table import sql_literal


class CoordinatorVocabulary(DeclaredFamily, declared_name=None):
    """A family root declaring the SQL fragments its members imply.

    Each root that composes this capability directly is one vocabulary; the
    coordinator schema reads them all from here, never from a list of families.
    """

    _vocabularies: ClassVar[list[type[CoordinatorVocabulary]]] = []

    def __init_subclass__(cls, **kwargs: str | None) -> None:
        super().__init_subclass__(**kwargs)
        if CoordinatorVocabulary in cls.__bases__:
            CoordinatorVocabulary._vocabularies.append(cls)

    @classmethod
    @abstractmethod
    def sql_vocabulary(cls) -> dict[str, str]:
        """The named SQL fragments this family's current members imply."""

    @staticmethod
    def schema_context() -> dict[str, str]:
        """Every vocabulary's SQL, rebuilt only when a family gains a member."""
        return _merged(
            *(
                vocabulary.members_with(vocabulary)
                for vocabulary in CoordinatorVocabulary._vocabularies
            )
        )

    @staticmethod
    def sql_values(names: Iterable[str]) -> str:
        return ",".join(sql_literal(name) for name in names)

    @classmethod
    def sql_members(cls, members: Iterable[type[DeclaredFamily]]) -> str:
        return cls.sql_values(member.declared_name for member in members)


@cache
def _merged(*members: tuple[type[DeclaredFamily], ...]) -> dict[str, str]:
    context: dict[str, str] = {}
    owners: dict[str, type[CoordinatorVocabulary]] = {}
    for vocabulary in CoordinatorVocabulary._vocabularies:
        for name, sql in vocabulary.sql_vocabulary().items():
            if name in owners:
                raise TypeError(
                    f"{name} is declared by both {owners[name].__name__} and {vocabulary.__name__}"
                )
            owners[name] = vocabulary
            context[name] = sql
    return context
