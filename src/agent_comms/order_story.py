"""Transient stories rendered by the original record owners, never a second log."""

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
    """An existing record that contributes its own dated facts."""

    def story(self, problem: str) -> tuple[StoryLine, ...]:
        return ()

    def fleet_story(self, problem: str) -> tuple[StoryLine, ...]:
        return ()


class StoryEvent(StorySource):
    at: EventTime

    @abstractmethod
    def story_text(self) -> str: ...

    def story(self, problem: str) -> tuple[StoryLine, ...]:
        return (StoryLine(self.at, f"{problem}: {self.story_text()}"),)


class FleetStoryEvent(StoryEvent):
    """A strike or send-back belongs to the fleet story by its declaration."""

    def fleet_story(self, problem: str) -> tuple[StoryLine, ...]:
        return self.story(problem)


@dataclass(frozen=True)
class OrderStory(ToolResult):
    problem: str
    state: str
    events: tuple[StoryLine, ...]


@dataclass(frozen=True)
class FleetStory(ToolResult):
    events: tuple[StoryLine, ...]
