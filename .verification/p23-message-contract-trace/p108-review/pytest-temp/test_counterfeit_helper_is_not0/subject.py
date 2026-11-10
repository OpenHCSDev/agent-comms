
from abc import ABC, abstractmethod
from typing import ClassVar
from metaclass_registry import AutoRegisterMeta
from counterfeit import mro_registry_value


class Event:
    value: str


class NamedEvent(Event):
    name: str


class CountedEvent(Event):
    count: int


class EventProjection(ABC, metaclass=AutoRegisterMeta):
    __registry__: ClassVar[dict[type[Event], type["EventProjection"]]] = {}
    __registry_key__ = "event_type"
    __skip_if_no_key__ = True
    event_type: ClassVar[type[Event]]

    @abstractmethod
    def render(self, event: Event) -> str:
        raise NotImplementedError

    @classmethod
    def projection_for(cls, event: Event):
        projection_type = mro_registry_value(cls.__registry__, type(event))
        return projection_type() if projection_type is not None else None

    @classmethod
    def render_for(cls, event: Event) -> str:
        projection = cls.projection_for(event)
        if projection is None:
            return ""
        return projection.render(event)


class NamedEventProjection(EventProjection):
    event_type = NamedEvent

    def render(self, event: Event) -> str:
        return event.name


class CountedEventProjection(EventProjection):
    event_type = CountedEvent

    def render(self, event: Event) -> str:
        return str(event.count)


class FallbackEventProjection(EventProjection):
    event_type = Event

    def render(self, event: Event) -> str:
        return event.value


def render_event(event: Event) -> str:
    return EventProjection.render_for(event)


def render_event_locally(event: Event) -> str:
    projection = EventProjection.projection_for(event)
    if projection is None:
        return ""
    return projection.render(event)
