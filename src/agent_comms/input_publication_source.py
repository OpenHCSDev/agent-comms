"""The original input-publication I/O, inherited by the actual bus reader."""

from __future__ import annotations

from pathlib import Path

from agent_comms.execution.input_root_authority import InputRootAuthority
from agent_comms.input_publication_read import InputPublicationRead

import agent_comms.execution.turn_context as turn_context
import agent_comms.input_attempt as input_attempt


class InputPublicationSource(
    InputPublicationRead[turn_context.Provenance, input_attempt.StoredInput],
):
    __slots__ = ()
    path: Path

    def input_publication(
        self, subject: turn_context.Provenance,
    ) -> tuple[InputRootAuthority, input_attempt.StoredInput]:
        privacy = registry_store.RegistryStore.at(self.path.parent).privacy()
        row = subject.require_human_input().require_original(
            input_disposition.InputDispositions(self.path.parent))
        return privacy, row


import agent_comms.input_disposition as input_disposition
import agent_comms.registry.registry_store as registry_store
