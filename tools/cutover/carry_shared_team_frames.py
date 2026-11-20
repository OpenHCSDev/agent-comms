"""Carry the actual flat HUD files into one shared atomic snapshot."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.nominal.shared_json import SharedJson
from agent_comms.storage.locked_store import PatternFile, RootFile, RuntimeFile
from agent_comms.team.team_frame_file import TeamBoardFile
from agent_comms.team.team_hud import (
    TeamBoardPublication,
    TeamBoardPublicationStore,
    TeamHud,
    TeamPlan,
)


class FlatTeamFrameFile(RuntimeFile, PatternFile):
    """The stored per-recipient JSON format this carry alone reads."""

    filename = "team_frames/{lookup}.json"


class SplitTeamPlanFile(RuntimeFile):
    """The split plan format read only by this one cutover."""

    filename = "team_plan.json"


class SharedTeamFrames(Carry, declared_name="shared_team_frames"):
    """Fold flat and split board formats into one atomic publication."""

    opened_files: ClassVar[tuple[type[RootFile], ...]] = (
        FlatTeamFrameFile, TeamBoardFile, SplitTeamPlanFile
    )

    def input_files(self) -> tuple[Path, ...]:
        return tuple(sorted({
            *super().input_files(),
            TeamBoardFile.path_in(self.root),
            SplitTeamPlanFile.path_in(self.root),
        }))

    def carry(self, *, apply: bool) -> Carried:
        destination = TeamBoardPublicationStore.at(self.root)
        plan = TeamPlan()
        retained: dict = {}
        existing_huds: tuple[TeamHud, ...] = ()
        if destination.path.exists():
            raw = FieldCodec.parse(destination.path.read_bytes())
            if isinstance(raw, dict) and "frames" in raw:
                publication = FieldCodec.decode(TeamBoardPublication, raw)
                plan = publication.plan
                existing_huds = destination.huds(publication=publication)
            else:
                frames = FieldCodec.decode(SharedJson, raw)
                existing_huds = FieldCodec.decode_shared(
                    tuple[TeamHud, ...], frames.expanded()
                )
            retained.update(
                (hud.teammate.require_recorded().lookup, hud)
                for hud in existing_huds
            )
        split_plan = SplitTeamPlanFile.path_in(self.root)
        if split_plan.exists():
            plan = FieldCodec.decode_document(
                TeamPlan, FieldCodec.parse(split_plan.read_bytes())
            )
        before = {
            hud.teammate: hud.with_plan(plan)
            for hud in existing_huds
        }
        sources = tuple(sorted(FlatTeamFrameFile.matching_in(self.root)))
        for source in sources:
            raw = FieldCodec.parse(source.read_bytes())
            if not isinstance(raw, dict):
                raise ValueError(f"HUD source {source} is not a record")
            if "phases" in raw:
                plan = FieldCodec.decode(TeamPlan, {"phases": raw.pop("phases")})
            hud = FieldCodec.decode(TeamHud, raw)
            lookup = hud.teammate.require_recorded().lookup
            if source.stem != str(lookup):
                raise ValueError(f"HUD source {source} belongs to another recipient")
            retained.setdefault(lookup, hud)
        changed = len(sources) + int(split_plan.exists())
        if apply and (changed or retained):
            destination.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination._publish_unlocked(
                destination.publication(
                    plan, tuple(retained.values()), before
                )
            )
            for source in sources:
                source.unlink()
            split_plan.unlink(missing_ok=True)
        rewritten = (*sources, split_plan, destination.path) if apply and (changed or retained) else ()
        return Carried(
            changed,
            rewritten=tuple(path.relative_to(self.root) for path in rewritten),
        )
