"""Carry the actual flat HUD files into one shared atomic snapshot."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.storage.locked_store import PatternFile, RootFile, RuntimeFile
from agent_comms.team.team_frame_file import TeamFrameFile
from agent_comms.team.team_hud import TeamFramesDocument, TeamHud


class FlatTeamFrameFile(RuntimeFile, PatternFile):
    """The stored per-recipient JSON format this carry alone reads."""

    filename = "team_frames/{lookup}.json"


class SharedTeamFrames(Carry, declared_name="shared_team_frames"):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (FlatTeamFrameFile, TeamFrameFile)

    def input_files(self) -> tuple[Path, ...]:
        # Include the destination before it exists, so rollback removes a new snapshot.
        return tuple(sorted({*super().input_files(), TeamFrameFile.path_in(self.root)}))

    def carry(self, *, apply: bool) -> Carried:
        sources = tuple(sorted(FlatTeamFrameFile.matching_in(self.root)))
        if not sources:
            return Carried(0)
        destination = TeamFramesDocument(TeamFrameFile.path_in(self.root))
        retained = {hud.teammate.require_recorded().lookup: hud for hud in destination.huds()}
        for source in sources:
            hud = FieldCodec.decode_document(TeamHud, FieldCodec.parse(source.read_bytes()))
            lookup = hud.teammate.require_recorded().lookup
            if source.stem != str(lookup):
                raise ValueError(f"HUD source {source} belongs to another recipient")
            # An existing new snapshot has already advanced beyond these old files.
            retained.setdefault(lookup, hud)
        if apply:
            destination.publish(tuple(retained.values()))
            for source in sources:
                source.unlink()
        return Carried(len(sources), rewritten=tuple(path.relative_to(self.root)
            for path in (*sources, destination.path)) if apply else ())
