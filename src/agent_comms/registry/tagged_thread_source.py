"""Original identity and tags consumed by namespace delivery selection."""

from agent_comms.registry.thread_identity_source import ThreadIdentitySource


class TaggedThreadSource(ThreadIdentitySource):
    __slots__ = ()

    tags: frozenset[str]
