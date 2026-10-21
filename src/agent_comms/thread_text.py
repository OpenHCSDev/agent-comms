"""Text that is given or absent, and the thread's own title, assignment and parent families."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection
from dataclasses import dataclass
from typing import ClassVar

from agent_comms.field_codec import NullMember, SingleFieldValue, SubclassUnion, boundary_decoder


class TextChoice(ABC):
    """A text value that was given, or was not; the reader supplies its own default."""

    __slots__ = ()

    @abstractmethod
    def otherwise[Default](self, default: Default) -> str | Default: ...

    @abstractmethod
    def require(self, missing: str) -> str:
        """The given text; the absent member refuses with ``missing``."""


class GivenText(SingleFieldValue, TextChoice):
    """The given member: its declared ``text`` field is the whole wire value."""

    __slots__ = ()
    text: str

    def otherwise[Default](self, default: Default) -> str:
        return self.text

    def require(self, missing: str) -> str:
        return self.text


class AbsentText(NullMember, TextChoice):
    """The absent member: JSON null, and the reader's default."""

    __slots__ = ()

    def otherwise[Default](self, default: Default) -> Default:
        return default

    def require(self, missing: str) -> str:
        raise ValueError(missing)


class ThreadText(SubclassUnion, TextChoice, affix="ThreadText"):
    """A thread's title or original assignment."""

    @staticmethod
    @boundary_decoder
    def external(text: str | None) -> ThreadText:
        """An imported record's title: its missing or empty text is no title."""
        return GivenThreadText(text) if text else NoThreadText()

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


class ThreadParent(SubclassUnion, TextChoice, affix="Parent"):
    """The thread this one was forked from, or none."""

    forked: ClassVar[bool]

    @abstractmethod
    def names(self) -> tuple[str, ...]: ...

    @abstractmethod
    def kept_in(self, names: Collection[str]) -> ThreadParent:
        """This parent while it is one of the names; otherwise no parent."""

    def named(self, name: str) -> bool:
        return name in self.names()


@dataclass(frozen=True, slots=True)
class ForkedParent(GivenText, ThreadParent):
    text: str
    forked = True

    def names(self) -> tuple[str, ...]:
        return (self.text,)

    def kept_in(self, names: Collection[str]) -> ThreadParent:
        return self if self.text in names else NoParent()


@dataclass(frozen=True, slots=True)
class NoParent(AbsentText, ThreadParent):
    """A thread that was registered or created, not forked."""

    forked = False

    def names(self) -> tuple[str, ...]:
        return ()

    def kept_in(self, names: Collection[str]) -> NoParent:
        return self
