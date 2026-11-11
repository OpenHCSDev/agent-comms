"""A phase's retained artifacts, identified by their reviewed Git blob contents."""

from __future__ import annotations

import json
from dataclasses import dataclass

from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord
from agent_comms.planning.phase_evidence import MainEvidence, MainTree, UnreadableEvidenceError
from agent_comms.projects.repo import Repo


@dataclass(frozen=True)
class RecordedArtifact(MainEvidence):
    """The manifest must name this required file at its recorded contents.

    Reviewing and recording an artifact establishes what it demonstrates. This check
    keeps those exact bytes on main; neither a prose marker nor a replacement file
    inherits the recorded evidence merely by occupying the same path.
    """

    manifest: str
    path: str

    def read(self, main: MainTree) -> bool:
        if not main.has(self.manifest):
            return False
        content, = Repo.blobs(main.repo.checkout, main.files[self.manifest])
        try:
            recorded = FieldCodec.decode(dict[str, str], json.loads(content))
        except (json.JSONDecodeError, UnicodeDecodeError, MalformedRecord) as error:
            raise UnreadableEvidenceError(f"Invalid artifact manifest {self.manifest}: {error}") from error
        return self.path in recorded and main.files.get(self.path) == recorded[self.path]

    def describe(self) -> str:
        return f"{self.path} at its Git blob recorded in {self.manifest}"
