"""Text that is given or absent, and the thread's own title and assignment family."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from agent_comms.field_codec import NullMember, SingleFieldValue, SubclassUnion


class TextChoice(ABC):
    """A text value that was given, or was not; the reader supplies its own default."""

    __slots__ = ()

    @abstractmethod
    def otherwise[Default](self, default: Default) -> str | Default: ...


class GivenText(SingleFieldValue, TextChoice):
    """The given member: its declared ``text`` field is the whole wire value."""

    __slots__ = ()
    text: str

    def otherwise[Default](self, default: Default) -> str:
        return self.text


class AbsentText(NullMember, TextChoice):
    """The absent member: JSON null, and the reader's default."""

    __slots__ = ()

    def otherwise[Default](self, default: Default) -> Default:
        return default


class ThreadText(SubclassUnion, TextChoice, affix="ThreadText"):
    """A thread's title or original assignment."""

    @abstractmethod
    def applied(self, previous: ThreadText) -> ThreadText:
        """The text a redeclaration keeps: its own, or the previous one."""

    @abstractmethod
    def texts(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class GivenThreadText(GivenText, ThreadText):
    text: str

    def applied(self, previous: ThreadText) -> GivenThreadText:
        return self

    def texts(self) -> tuple[str, ...]:
        return (self.text,)


@dataclass(frozen=True, slots=True)
class NoThreadText(AbsentText, ThreadText):
    """No title or original assignment was supplied for this thread."""

    def applied(self, previous: ThreadText) -> ThreadText:
        return previous

    def texts(self) -> tuple[str, ...]:
        return ()
