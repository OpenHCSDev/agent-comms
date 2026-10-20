"""Transient story views rendered by the original Todo record owners."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from .recorded_time import EventTime
from .tool_result import ToolResult


@dataclass(frozen=True)
class StoryLine:
    at: EventTime
    text: str


class StorySource:
    def story(self) -> tuple[StoryLine, ...]:
        return ()


class StoryEvent(StorySource):
    at: EventTime

    @abstractmethod
    def story_text(self) -> str: ...

    def story(self) -> tuple[StoryLine, ...]:
        return (StoryLine(self.at, self.story_text()),)


@dataclass(frozen=True)
class OrderStory(ToolResult):
    problem: str
    state: str
    events: tuple[StoryLine, ...]

    @property
    def line(self) -> str:
        return f"{self.problem}: {self.state}; " + "; ".join(event.text for event in self.events)


@dataclass(frozen=True)
class FleetStory(ToolResult):
    lines: tuple[str, ...]
