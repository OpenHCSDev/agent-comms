"""The original input-publication I/O, inherited by the actual bus reader."""

from __future__ import annotations

from pathlib import Path

import agent_comms.execution.turn_context as turn_context
import agent_comms.input_attempt as input_attempt
import agent_comms.input_disposition as input_disposition
import agent_comms.registry.registry_store as registry_store
from agent_comms.execution.input_publication_read import InputPublicationRead
from agent_comms.execution.input_root_authority import InputRootAuthority


class InputPublicationSource(
    InputPublicationRead[turn_context.Provenance, input_attempt.StoredInput],
):
    __slots__ = ()
    path: Path

    def input_publication(
        self, subject: turn_context.Provenance,
    ) -> tuple[InputRootAuthority, input_attempt.StoredInput]:
        privacy = registry_store.RegistryStore.at(self.path.parent).privacy()
        provenance = subject.require_human_input()
        inputs = input_disposition.InputDispositions(self.path.parent)
        row = inputs.lookup(provenance.key).require_original_provenance(provenance)
        return privacy, row
