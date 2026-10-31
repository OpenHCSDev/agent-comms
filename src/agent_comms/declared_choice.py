"""Named family members used as choices across backend and user interfaces."""
from typing import ClassVar

from agent_comms.nominal.declared_family import DeclaredFamily


class DeclaredChoice(DeclaredFamily):
    @classmethod
    def choice_label(cls) -> str:
        return cls.declared_name.replace("-", " ").replace("_", " ").title()


class TitledChoice:
    """A declared display title replaces the humanized family name."""
    title: ClassVar[str]

    @classmethod
    def choice_label(cls) -> str:
        return cls.title
