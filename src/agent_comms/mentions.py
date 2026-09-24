"""Portable thread mentions: textual addressees, independent of message delivery."""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

_NAME = r"[A-Za-z0-9_-]"
_MENTION = re.compile(r"(?<![\w@/\\])@(" + _NAME + r"+)")
_PENDING = re.compile(r"(?<![\w@/\\])@(" + _NAME + r"*)$")
_SUFFIX = re.compile(_NAME + "*")


@dataclass(frozen=True, slots=True)
class MentionCandidate:
    name: str
    title: str


@dataclass(frozen=True, slots=True)
class ThreadMention:
    thread: str
    start: int
    end: int

    @classmethod
    def find(cls, body: str, resolve: Callable[[str], str | None]) -> tuple["ThreadMention", ...]:
        return tuple(
            cls(name, match.start(), match.end())
            for match in _MENTION.finditer(body)
            if (name := resolve(match.group(1))) is not None
        )

    @classmethod
    def from_wire(cls, value: Mapping) -> "ThreadMention":
        return cls(str(value["thread"]), int(value["start"]), int(value["end"]))


@dataclass(frozen=True, slots=True)
class MentionQuery:
    start: int
    end: int
    prefix: str

    @classmethod
    def at_cursor(cls, text: str, cursor: int) -> "MentionQuery | None":
        if not 0 <= cursor <= len(text):
            return None
        match = _PENDING.search(text[:cursor])
        if match is None:
            return None
        suffix = _SUFFIX.match(text, cursor)
        return cls(match.start(), suffix.end() if suffix else cursor, match.group(1))

    def candidates(self, values: Sequence[MentionCandidate]) -> tuple[MentionCandidate, ...]:
        return tuple(
            value for value in values if value.name.casefold().startswith(self.prefix.casefold())
        )
