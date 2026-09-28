"""Participant rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field

from agent_comms.coordination_schema import CoordinatorTable
from agent_comms.typed_table import (
    Column,
    ForeignKey,
    TypedTable,
)


@dataclass(frozen=True, kw_only=True)
class Participants(CoordinatorTable, TypedTable):
    participant_lookup: str = dataclass_field(
        metadata={
            "sql": Column(
                primary_key=True,
                check="\n        length(participant_lookup) BETWEEN 1 AND 256\n    ",
            )
        }
    )
    display_name: str = dataclass_field(
        metadata={"sql": Column(check="length(display_name) BETWEEN 1 AND 256")}
    )
    committed: bool

    @classmethod
    def triggers(cls):
        return {
            "participant_identity_immutable": (
                """CREATE TRIGGER participant_identity_immutable
BEFORE UPDATE OF participant_lookup ON participants
BEGIN
    SELECT RAISE(ABORT, 'participant lookup identity is immutable');
END"""
            ),
            "participant_commit_monotonic": (
                """CREATE TRIGGER participant_commit_monotonic
BEFORE UPDATE OF committed ON participants
WHEN OLD.committed = 1 AND NEW.committed = 0
BEGIN
    SELECT RAISE(ABORT, 'participant commitment cannot be revoked');
END"""
            ),
            "participant_identity_delete_frozen": (
                """CREATE TRIGGER participant_identity_delete_frozen
BEFORE DELETE ON participants
BEGIN
    SELECT RAISE(ABORT, 'participant lookup identity cannot be deleted');
END"""
            ),
        }


@dataclass(frozen=True, kw_only=True)
class ParticipantAliases(CoordinatorTable, TypedTable):
    alias: str = dataclass_field(
        metadata={"sql": Column(primary_key=True, check="length(alias) BETWEEN 1 AND 256")}
    )
    participant_lookup: str
    renamed_at_ms: int = dataclass_field(metadata={"sql": Column(check="renamed_at_ms >= 0")})

    @classmethod
    def references(cls):
        return (
            ForeignKey(
                ("participant_lookup",),
                Participants,
                ("participant_lookup",),
                deferred=False,
                on_delete=None,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "participant_alias_identity_immutable": (
                """CREATE TRIGGER participant_alias_identity_immutable
BEFORE UPDATE ON participant_aliases
BEGIN
    SELECT RAISE(ABORT, 'participant alias identity is immutable');
END"""
            ),
            "participant_alias_delete_frozen": (
                """CREATE TRIGGER participant_alias_delete_frozen
BEFORE DELETE ON participant_aliases
BEGIN
    SELECT RAISE(ABORT, 'participant alias cannot be deleted');
END"""
            ),
        }


@dataclass(frozen=True, kw_only=True)
class OwnerGenerations(CoordinatorTable, TypedTable):
    owner_lookup: str = dataclass_field(metadata={"sql": Column(primary_key=True)})
    owner_thread: str = dataclass_field(
        metadata={"sql": Column(check="length(owner_thread) BETWEEN 1 AND 256")}
    )
    generation: int = dataclass_field(metadata={"sql": Column(check="generation > 0")})

    @classmethod
    def references(cls):
        return (
            ForeignKey(
                ("owner_lookup",),
                Participants,
                ("participant_lookup",),
                deferred=False,
                on_delete=None,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "owner_generation_delete_frozen": (
                """CREATE TRIGGER owner_generation_delete_frozen BEFORE DELETE ON
owner_generations BEGIN
    SELECT RAISE(ABORT, 'owner generation counter cannot be deleted'
    );
END"""
            ),
            "owner_generation_monotonic": (
                """CREATE TRIGGER owner_generation_monotonic BEFORE UPDATE ON owner_generations
WHEN NEW.owner_lookup IS NOT OLD.owner_lookup
 OR NEW.generation != OLD.generation + 1
 OR EXISTS (SELECT 1 FROM attempts WHERE owner_lookup = OLD.owner_lookup
            AND owner_generation = OLD.generation
            AND phase NOT IN ({terminal_attempt_names}))
BEGIN SELECT RAISE(ABORT, 'owner generation cannot advance with active attempts'); END"""
            ),
        }
