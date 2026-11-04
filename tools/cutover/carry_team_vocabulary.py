"""Rename the lead's stored vocabulary to the team's.

The team has no lead, so its stores stop naming one: a record answers for its ``teammate``,
team packets, rules, plans and stores say ``team``, a fork runs a plain ``fork_execution``, and
the coordination channel is ``#teammates``. Every row and file is kept; only names change.
Message targets stay as sent, as a channel rename leaves them. Delete these steps after the live
installation.
"""

from __future__ import annotations

import shutil
from contextlib import closing
from typing import ClassVar

from pydantic import JsonValue

from agent_comms.bus import Bus
from agent_comms.catalog_store import ChannelCatalog
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.messages import EscalationPosition, Message
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.service_passes import ServicePassStore
from agent_comms.store_files import AtomicFile
from agent_comms.task_attachments.team_pings import TeamUpdateStore
from agent_comms.todos import HuntRecord, OwnerRequest, TodoStore
from agent_comms.typed_table import TypedTable


class TeamVocabulary(Carry, declared_name=None):
    """One renaming shared by every store that wrote the lead's names."""

    tags: ClassVar[dict[str, str]] = {
        "commander_notice": "team_notice", "commander_decisions": "team_decisions",
        "commander_deaf": "team_deaf", "commander_heartbeat": "team_heartbeat",
        "commander_native": "team_native", "commander_page": "team_page",
        "commander_latency": "team_latency", "commander_overloaded": "team_overloaded",
        "commander_fork_execution": "fork_execution", "awaiting_commander": "awaiting_team",
        "commanders": "teammates", "#commanders": "#teammates",
    }
    """Stored declared names and the channel tag, matched as whole strings."""
    keys: ClassVar[dict[str, str]] = {"commander": "teammate", "commander_completed": "teammate_completed"}
    """Stored record fields."""

    @classmethod
    def renamed(cls, value: JsonValue) -> JsonValue:
        match value:
            case dict():
                return {cls.tags.get(key, cls.keys.get(key, key)): cls.renamed(item)
                        for key, item in value.items()}
            case list():
                return [cls.renamed(item) for item in value]
            case str():
                return cls.tags.get(value, value)
        return value

    @classmethod
    def renamed_text(cls, text: str) -> str | None:
        """The rewritten JSON text, or None when nothing in it names the lead."""
        stored = FieldCodec.parse(text)
        renamed = cls.renamed(stored)
        return None if renamed == stored else FieldCodec.compact(renamed, allow_nan=False)

    @classmethod
    def rewrite_rows(cls, db, row: type[TypedTable]) -> int:
        """Inside the caller's transaction, every JSON-valued column of ``row`` that names the
        lead, keyed by the row's primary key."""
        keys = tuple(field for field in row._fields if field.column.primary_key)
        selected = ", ".join(f'"{key.wire_name}"' for key in keys)
        changed = 0
        for field in row._fields:
            if field in keys:
                continue
            stored = db.execute(
                f'SELECT "{field.wire_name}", {selected} FROM "{row.declared_name}"'
                f""" WHERE "{field.wire_name}" LIKE '%commander%'""").fetchall()
            updates = []
            for text, *identity in stored:
                if isinstance(text, str) and text[:1] in "[{\"":
                    if (renamed := cls.renamed_text(text)) is not None:
                        updates.append((renamed, *identity))
            if updates:
                cls.rewrite_stored(db, row, FieldCodec.field_plan(row, field.name), updates)
                changed += len(updates)
        return changed


class TeamVocabularyMessages(TeamVocabulary):
    """Team packets, notices, rules and states in message tasks; the packet projections are
    dropped under their old names and rebuilt from history."""

    opened_files: ClassVar[tuple[type[RootFile], ...]] = (Bus,)
    retired_projections: ClassVar[tuple[str, ...]] = ("commander_update_position",)

    def carry(self, *, apply: bool) -> Carried:
        table = Message.table
        column = FieldCodec.field_plan(table, "task")
        changed = 0
        rewritten = []
        for path in self.input_files():
            with closing(Bus.read_snapshot(path)) as db:
                stored = db.execute(table._sql(
                    f'SELECT {{seq}}, {{task}} FROM "{table.declared_name}"'
                    " WHERE {task} LIKE '%commander%'")).fetchall()
                projections = tuple(name for name in self.retired_projections if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())
                escalations = self.escalation_column(db)
            updates = [(renamed, seq) for seq, task in stored
                       if (renamed := self.renamed_text(task)) is not None]
            if not (updates or projections or escalations):
                continue
            changed += len(updates) + len(projections) + escalations
            if apply:
                with closing(Bus.writer(path)) as db, Bus.write_transaction(db):
                    if updates:
                        self.rewrite_stored(db, table, column, updates)
                    for name in projections:
                        db.execute(f'DROP TABLE "{name}"')
                    if escalations:
                        db.execute(f'DROP TABLE "{EscalationPosition.declared_name}"')
                rewritten.append(path.relative_to(self.root))
        return Carried(changed, rewritten=tuple(rewritten))

    @staticmethod
    def escalation_column(db) -> int:
        """The escalation projection keyed by the old column: dropped, then rebuilt."""
        present = EscalationPosition.present(db)
        return int(present and "commander_lookup" in EscalationPosition.stored_columns(db))


class TeamVocabularyTodos(TeamVocabulary):
    """Owner requests and hunts answer for a teammate; orders serve team plan phases."""

    opened_files: ClassVar[tuple[type[RootFile], ...]] = (TodoStore,)
    columns: ClassVar[tuple[tuple[type[TypedTable], str, str], ...]] = (
        (OwnerRequest, "commander", "teammate"),
        (HuntRecord, "commander", "teammate"),
    )
    """Each table rebuilds from its declaration, which renames its generated column and
    indexes with the field."""

    def carry(self, *, apply: bool) -> Carried:
        changed = 0
        rewritten = []
        for path in self.input_files():
            with closing(TodoStore.read_snapshot(path)) as db:
                renames = tuple((row, old, new) for row, old, new in self.columns
                                if row.present(db) and old in row.stored_columns(db))
                rows = tuple(row for row in TodoStore.schema_rows() if row.present(db))
                tagged = sum(db.execute(
                    f'SELECT COUNT(*) FROM "{row.declared_name}" WHERE '
                    + " OR ".join(f""""{column}" LIKE '%commander%'""" for column in row.stored_columns(db))
                    ).fetchone()[0] for row in rows)
            if not (renames or tagged):
                continue
            changed += len(renames) + tagged
            if apply:
                with closing(TodoStore.writer(path)) as db, TodoStore.write_transaction(db):
                    for row, old, new in renames:
                        row.rebuild(db, {new: f'"{old}"'})
                    for row in rows:
                        self.rewrite_rows(db, row)
                rewritten.append(path.relative_to(self.root))
        return Carried(changed, rewritten=tuple(rewritten))


class TeamVocabularyFiles(TeamVocabulary):
    """Each teammate's retained packet moves to ``team_updates/``, the service passes and the
    channel catalog drop the lead's names, and the runtime frames under the old directory go;
    the heartbeat writes them again on its next pass."""

    opened_files: ClassVar[tuple[type[RootFile], ...]] = (ServicePassStore, ChannelCatalog)
    retired_updates: ClassVar[str] = "commander_updates"
    retired_frames: ClassVar[str] = "commander_frames"

    def carry(self, *, apply: bool) -> Carried:
        documents = {path: renamed for path in self.input_files()
                     if (renamed := self.renamed_text(path.read_text(encoding="utf-8"))) is not None}
        moves = {}
        updates = self.root / self.retired_updates
        if updates.is_dir():
            for old in sorted(updates.glob("*.json")):
                self.require_confined(old)
                text = old.read_text(encoding="utf-8")
                new = TeamUpdateStore.path_in(self.root, lookup=old.stem)
                moves[old] = (new, self.renamed_text(text) or text)
        frames = self.root / self.retired_frames
        changed = len(documents) + len(moves) + int(frames.is_dir())
        if apply:
            for path, text in documents.items():
                AtomicFile(path).write_text(text)
            for old, (new, text) in moves.items():
                new.parent.mkdir(parents=True, exist_ok=True)
                AtomicFile(new).write_text(text)
                old.unlink()
            if updates.is_dir():
                updates.rmdir()
            if frames.is_dir():
                self.require_confined(frames)
                shutil.rmtree(frames)
        rewritten = (*documents, *(new for new, _ in moves.values()))
        return Carried(changed, rewritten=tuple(path.relative_to(self.root) for path in rewritten)
                       if apply else ())

