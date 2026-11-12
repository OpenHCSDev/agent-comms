from __future__ import annotations

import itertools
import re
from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import (
    Collection,
    Iterator,
)
from dataclasses import dataclass
from typing import ClassVar

from agent_comms.execution.history_view import ForkOwnHistory, HistoryView, OwnHistory
from agent_comms.nominal.field_codec import (
    NullMember,
    SingleFieldValue,
    StoredSessionId,
    SubclassUnion,
    boundary_decoder,
)


class TextChoice(ABC):
    """A text value that was given, or was not; the reader supplies its own default."""

    __slots__ = ()

    @abstractmethod
    def otherwise[Default](self, default: Default) -> str | Default: ...

    @abstractmethod
    def require(self, missing: str) -> str:
        """The given text; the absent member refuses with ``missing``."""

    @abstractmethod
    def texts(self) -> tuple[str, ...]: ...


class GivenText(SingleFieldValue, TextChoice):
    """The given member: its declared ``text`` field is the whole wire value."""

    __slots__ = ()
    text: str

    def otherwise[Default](self, default: Default) -> str:
        return self.text

    def require(self, missing: str) -> str:
        return self.text

    def texts(self) -> tuple[str, ...]:
        return (self.text,)


class AbsentText(NullMember, TextChoice):
    """The absent member: JSON null, and the reader's default."""

    __slots__ = ()

    def otherwise[Default](self, default: Default) -> Default:
        return default

    def require(self, missing: str) -> str:
        raise ValueError(missing)

    def texts(self) -> tuple[str, ...]:
        return ()


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


@dataclass(frozen=True, slots=True)
class GivenThreadText(GivenText, ThreadText):
    text: str

    def applied(self, previous: ThreadText) -> GivenThreadText:
        return self


@dataclass(frozen=True, slots=True)
class NoThreadText(AbsentText, ThreadText):
    """No title or original assignment was supplied for this thread."""

    def applied(self, previous: ThreadText) -> ThreadText:
        return previous


class ThreadParent(SubclassUnion, TextChoice, affix="Parent"):
    """The thread this one was forked from, or none."""

    own_history: ClassVar[HistoryView]

    @abstractmethod
    def is_forked(self) -> bool: ...
    """How this thread shows its own history."""

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
    own_history = ForkOwnHistory()

    def is_forked(self) -> bool:
        return True

    def names(self) -> tuple[str, ...]:
        return (self.text,)

    def kept_in(self, names: Collection[str]) -> ThreadParent:
        return self if self.text in names else NoParent()


@dataclass(frozen=True, slots=True)
class NoParent(AbsentText, ThreadParent):
    """A thread that was registered or created, not forked."""

    own_history = OwnHistory()

    def is_forked(self) -> bool:
        return False

    def names(self) -> tuple[str, ...]:
        return ()

    def kept_in(self, names: Collection[str]) -> NoParent:
        return self


@dataclass(frozen=True, slots=True)
class SubagentRole(SingleFieldValue):
    """A subagent's role as its harness names it (Claude Code's `agent_type`). The role is the
    durable half of a child's name: each occupant of a role takes the first vacant slot."""

    text: str

    def __post_init__(self) -> None:
        if not self.slug:
            raise ValueError(
                f"Subagent role {self.text!r} has no letters or digits to name a slot.")

    @property
    def slug(self) -> str:
        """The role in the characters a thread name allows: `Explore` and `plugin:Code Review`
        become `explore` and `plugin-code-review`."""
        return "-".join(re.findall(r"[a-z0-9]+", self.text.lower()))

    def slot_names(self, parent: str) -> Iterator[str]:
        """`{parent}-{role}`, then `-2`, `-3`, ... for occupants running at once."""
        base = f"{parent}-{self.slug}"
        yield base
        for ordinal in itertools.count(2):
            yield f"{base}-{ordinal}"


class ExpectedSession(SubclassUnion, TextChoice, affix="Session"):
    """The session a stored history must hold, when the import names one."""

    @abstractmethod
    def holds(self, session: StoredSessionId) -> bool: ...


@dataclass(frozen=True, slots=True)
class NamedSession(GivenText, ExpectedSession):
    text: str

    def holds(self, session: StoredSessionId) -> bool:
        return session == self.text


@dataclass(frozen=True, slots=True)
class AnySession(AbsentText, ExpectedSession):
    def holds(self, session: StoredSessionId) -> bool:
        return True
