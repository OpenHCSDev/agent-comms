"""Input-publication reads without opening a store or importing its implementation."""

from abc import ABC, abstractmethod

from agent_comms.execution.input_root_authority import InputRootAuthority


class InputPublicationRead[Subject, Original](ABC):
    __slots__ = ()

    @abstractmethod
    def input_publication(self, subject: Subject) -> tuple[InputRootAuthority, Original]: ...
