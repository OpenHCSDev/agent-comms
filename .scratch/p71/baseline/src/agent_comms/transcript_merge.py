"""Transcript declarations own whether an existing streamed presentation may grow."""

from dataclasses import replace


class EventMerge:
    def merge(self, other):
        """Return a replacement event only when this declaration supports streaming."""
        return None


class StreamingMerge(EventMerge):
    def merge(self, other):
        # Dataclass equality proves all non-text facts, including routing,
        # still identify the same event. Distinct declarations never merge.
        if type(other) is not type(self):
            return None
        return other if replace(self, text=other.text) == other else None
