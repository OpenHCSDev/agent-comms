"""Compatibility tags derived from state declarations, never a second roster.

The durable SQLite format and older callers use scalar tags. Records contain
the nominal state; these tags only preserve that boundary during integration.
"""

from enum import StrEnum


def state_tags(name, family):
    class Tag(StrEnum):
        @property
        def declaration(self):
            return family.decode(self.value)

        @classmethod
        def _missing_(cls, value):
            # Test/plugin declarations added after import use the same authority.
            family.decode(value)
            member = str.__new__(cls, value)
            member._name_ = value.upper()
            member._value_ = value
            return member

    return Tag(name, {name.upper(): name for name in family.names()})


def transition_tags(tags, family):
    return {
        tags(name): frozenset(tags(nxt) for nxt in successors)
        for name, successors in family.transition_table().items()
    }
