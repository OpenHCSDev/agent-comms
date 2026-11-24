"""Portable thread mentions: textual addressees, independent of message delivery."""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agent_comms.messaging.projection_source import ProducedRow, ProjectionSource
from agent_comms.nominal.class_fact import ClassFact

_NAME = r"[A-Za-z0-9_-]"


_MENTION = re.compile(r"(?<![\w@/\\])@(" + _NAME + r"+)")


_CODE_SPAN = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)


_PENDING = re.compile(r"(?<![\w@/\\])@(" + _NAME + r"*)$")


_SUFFIX = re.compile(_NAME + "*")


def _inside_code_span(index: int, spans: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= index < end for start, end in spans)


@dataclass(frozen=True, slots=True)
class MentionCandidate:
    name: str
    title: str


@dataclass(frozen=True, slots=True)
class ThreadMention(ProjectionSource):

    @ClassFact
    def writes(cls) -> tuple[type[ProducedRow], ...]:
        from agent_comms.messaging.bus_display_index import BusDisplayIndex
        return (BusDisplayIndex,)

    thread: str
    start: int
    end: int

    @classmethod
    def find(cls, body: str, resolve: Callable[[str], str | None]) -> tuple[ThreadMention, ...]:
        code_spans = tuple(match.span() for match in _CODE_SPAN.finditer(body))
        return tuple(
            cls(name, match.start(), match.end())
            for match in _MENTION.finditer(body)
            if not _inside_code_span(match.start(), code_spans)
            and (name := resolve(match.group(1))) is not None
        )


@dataclass(frozen=True, slots=True)
class MentionQuery:
    start: int
    end: int
    prefix: str

    @classmethod
    def at_cursor(cls, text: str, cursor: int) -> MentionQuery | None:
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
