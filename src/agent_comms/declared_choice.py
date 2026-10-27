"""Named family members used as choices across backend and user interfaces."""
from typing import ClassVar

from agent_comms.declared_family import DeclaredFamily


class DeclaredChoice(DeclaredFamily):
    title: ClassVar[str | None] = None

    @classmethod
    def choice_label(cls) -> str:
        return cls.title or cls.declared_name.replace("-", " ").replace("_", " ").title()
