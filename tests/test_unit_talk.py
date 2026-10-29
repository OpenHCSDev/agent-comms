from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from agent_comms.commander.unit_talk import BlockedTalk, Ceremony, UnitTalk
from agent_comms.nominal.field_codec import MessageId
from agent_comms.thread_identity import RecordedThreadIncarnation

UNIT = RecordedThreadIncarnation("install-speed", 1792869186.2298183)


@dataclass(frozen=True)
class Talk(UnitTalk):
    """The real window over fixed messages and commits instead of a live bus."""

    said: tuple[SimpleNamespace, ...] = ()
    committed: int = 0
    holding: bool = True
    source: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(
        thread=SimpleNamespace(incarnation=UNIT), now=0.0, pending=(), sources=()))

    @property
    def messages(self):
        return self.said

    @property
    def commits(self) -> int:
        return self.committed

    def __post_init__(self) -> None:
        self.source.pending = ("P964",) if self.holding else ()


def said(body: str, mentions: tuple = ()) -> SimpleNamespace:
    return SimpleNamespace(body=body, text_size=len(body), timestamp=10.0, mentions=mentions,
                           message_id=MessageId("0123456789ab"))


@pytest.mark.parametrize("body", (
    "Source delivered; awaiting root review.",
    "Published pair frozen for your review.",
    "I'm still waiting on the judge's verdict for #7.",
))
def test_an_unowned_wait_is_a_named_row(body: str) -> None:
    row, = BlockedTalk.derive(Talk(said=(said(body),)))
    assert row.holder == UNIT and row.message_id == MessageId("0123456789ab")


@pytest.mark.parametrize("message", (
    said("waiting on @codex-greentext to record the batch"),
    said("blocked on src/agent_comms/todos.py:4114 refusing the batch"),
    said("not waiting on this conflict; continuing"),
    said("Origin wasn't waiting for a roll call."),
    said("preserving the existing behaviour; the author freeze is superseded"),
    said("waiting for the reviewer", mentions=("root",)),
))
def test_an_owned_negated_or_ordinary_message_is_not(message: SimpleNamespace) -> None:
    assert tuple(BlockedTalk.derive(Talk(said=(message,)))) == ()


def test_ceremony_counts_characters_per_commit_while_holding_work() -> None:
    loud = (said("x" * (Ceremony.per_commit * 2 + 1)),)
    row, = Ceremony.derive(Talk(said=loud, committed=2))
    assert (row.characters, row.commits) == (Ceremony.per_commit * 2 + 1, 2)
    assert tuple(Ceremony.derive(Talk(said=loud, committed=3))) == ()
    assert tuple(Ceremony.derive(Talk(said=loud, committed=0, holding=False))) == ()
