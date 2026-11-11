"""Identity reads shared by the original recorded and live thread declarations."""


from agent_comms.registry.thread_creation import ThreadCreationSource
from agent_comms.registry.thread_incarnation_read import ReadsThreadIncarnation
from agent_comms.registry.thread_identity import ThreadIncarnation


class ThreadIdentitySource(ThreadCreationSource, ReadsThreadIncarnation[ThreadIncarnation]):
    __slots__ = ()

    name: str

