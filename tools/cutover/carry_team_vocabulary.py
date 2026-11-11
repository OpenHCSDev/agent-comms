"""Rename the lead's stored vocabulary to the team's.

The team has no lead, so its stores stop naming one: a record answers for its ``teammate``,
team packets, rules, plans and stores say ``team``, a fork runs a plain ``fork_execution``, and
the coordination channel is ``#teammates``. Every row and file is kept; only names change.
Message targets stay as sent, as a channel rename leaves them. Delete these steps after the live
installation.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import ClassVar

from agent_comms.coordination.service_passes import ServicePassStore
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.messages import EscalationPosition, Message
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.planning.orders import (
    HuntRecord,
    OwnerRequest,
    TodoStore,
)
from agent_comms.registry.catalog_store import ChannelCatalog
from agent_comms.storage.locked_store import PatternFile, RootFile
from agent_comms.storage.store_files import AtomicFile, is_store_lock
from agent_comms.storage.typed_table import TypedTable
from agent_comms.task_attachments.team_pings import TeamUpdateStore
from agent_comms.team.team_frame_file import TeamFrameFile


class TeamVocabulary(Carry, declared_name=None):
    """One renaming shared by every store that wrote the lead's names."""

    tags: ClassVar[dict[str, str]] = {
        "commander_notice": "team_notice",
        "commander_decisions": "team_decisions",
        "commander_deaf": "team_deaf",
        "commander_heartbeat": "team_heartbeat",
        "commander_native": "team_native",
        "commander_page": "team_page",
        "commander_latency": "team_latency",
        "commander_overloaded": "team_overloaded",
        "commander_fork_execution": "fork_execution",
        "awaiting_commander": "awaiting_team",
        "commanders": "teammates",
        "#commanders": "#teammates",
    }
    """Stored declared names and the channel tag, matched as whole strings."""
    keys: ClassVar[dict[str, str]] = {
        "commander": "teammate",
        "commander_completed": "teammate_completed",
    }
    """Stored record fields."""
    token: ClassVar[re.Pattern[str]] = re.compile(r'"((?:[^"\\]|\\.)*)"(\s*:?)')
    """One JSON string token and, when it is an object key, its colon. Outside a string JSON
    has no quote, so scanning stored JSON left to right meets every string exactly once."""

    @classmethod
    def renamed_text(cls, text: str) -> str:
        """Stored JSON with every whole string that names the lead renamed, keys also taking the
        field renames; the rest of the text, its formatting included, stays as stored."""
        return cls.token.sub(cls.renamed_token, text)

    @classmethod
    def renamed_token(cls, match: re.Match[str]) -> str:
        name, colon = match.group(1), match.group(2)
        names = cls.keys | cls.tags if colon.strip() else cls.tags
        return f'"{names.get(name, name)}"{colon}'

    @classmethod
    def rewrite_rows(
        cls,
        db: sqlite3.Connection,
        row: type[TypedTable],
        *,
        apply: bool = True,
    ) -> int:
        """Count the actual rewrites, optionally applying them in the caller's transaction.

        The storage family exposes JSON records; a text field stays text even when its
        contents happen to look like JSON. Fields renamed by a pending rebuild are absent
        from this source schema, not columns to reinterpret as SQLite string literals.
        """
        keys = tuple(field for field in row._fields if field.column.primary_key)
        selected = ", ".join(f'"{key.wire_name}"' for key in keys)
        changed = 0
        stored_columns = row.stored_columns(db)
        for field in row._fields:
            if field in keys or field.wire_name not in stored_columns:
                continue
            column = f'"{field.wire_name}"'
            encoded = field.storage.json_text(db, column)
            stored = db.execute(
                f'SELECT {column}, {selected} FROM "{row.declared_name}"'
                f" WHERE {column} LIKE '%commander%' AND json_valid({encoded})"
                f" AND json_type({encoded}) IN ('object', 'array')"
            ).fetchall()
            updates = [
                (renamed, *identity)
                for text, *identity in stored
                if (renamed := cls.renamed_text(text)) != text
            ]
            if updates:
                if apply:
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
                stored = db.execute(
                    table._sql(
                        f'SELECT {{seq}}, {{task}} FROM "{table.declared_name}"'
                        " WHERE {task} LIKE '%commander%'"
                    )
                ).fetchall()
                projections = tuple(
                    name
                    for name in self.retired_projections
                    if db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
                    ).fetchone()
                )
                escalations = self.escalation_column(db)
            updates = [
                (renamed, seq)
                for seq, task in stored
                if (renamed := self.renamed_text(task)) != task
            ]
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
    def escalation_column(db: sqlite3.Connection) -> int:
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
                renames = tuple(
                    (row, old, new)
                    for row, old, new in self.columns
                    if row.present(db) and old in row.stored_columns(db)
                )
                rows = tuple(row for row in TodoStore.schema_rows() if row.present(db))
                tagged = sum(self.rewrite_rows(db, row, apply=False) for row in rows)
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
    """The service passes and the channel catalog drop the lead's names, and each teammate's
    retained packet and HUD frame move from the lead's directory to the team's, renamed: a HUD
    read needs its frame before the heartbeat's next pass writes one."""

    opened_files: ClassVar[tuple[type[RootFile], ...]] = (ServicePassStore, ChannelCatalog)
    retired: ClassVar[tuple[tuple[str, type[PatternFile]], ...]] = (
        ("commander_updates", TeamUpdateStore),
        ("commander_frames", TeamFrameFile),
    )
    """Each old per-teammate directory and the store its files now live in."""

    def moved(self) -> dict[Path, Path]:
        """Each retired document and the home it moves to."""
        return {
            old: store.path_in(self.root, lookup=old.stem)
            for name, store in self.retired
            for old in sorted((self.root / name).glob("*.json"))
        }

    def input_files(self) -> tuple[Path, ...]:
        """A moved document and its new home are inputs like any store: the practice copy holds
        them so the rehearsal runs these moves, and a failed install restores the document and
        removes what the move wrote."""
        moved = self.moved()
        for path in (*moved, *moved.values()):
            self.require_confined(path)
        return tuple(sorted({*super().input_files(), *moved, *moved.values()}))

    def carry(self, *, apply: bool) -> Carried:
        stored = {path: path.read_text(encoding="utf-8") for path in super().input_files()}
        documents = {
            path: renamed
            for path, text in stored.items()
            if (renamed := self.renamed_text(text)) != text
        }
        moves = {
            old: (new, self.renamed_text(old.read_text(encoding="utf-8")))
            for old, new in self.moved().items()
        }
        if apply:
            for path, text in documents.items():
                AtomicFile(path).write_text(text)
            for old, (new, text) in moves.items():
                new.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # as the stores do
                AtomicFile(new).write_text(text)
                old.unlink()
        rewritten = (*documents, *moves, *(new for new, _ in moves.values()))
        return Carried(
            len(documents) + len(moves),
            rewritten=tuple(path.relative_to(self.root) for path in rewritten) if apply else (),
        )

    def after_acceptance(self) -> Carried:
        """A restore needs the directories to put documents back into, so they outlive the
        rollback window. Their lock files are not content and no carry moves them; anything else
        left there stops the removal."""
        directories = tuple(
            self.root / name for name, _ in self.retired if (self.root / name).is_dir())
        for directory in directories:
            for lock in filter(is_store_lock, directory.iterdir()):
                lock.unlink()
            directory.rmdir()
        return Carried(len(directories))
