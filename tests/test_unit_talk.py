from dataclasses import dataclass, field
from types import SimpleNamespace

from agent_comms.commander.unit_talk import Ceremony, UnitTalk
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
    return SimpleNamespace(body=body, text_size=len(body), timestamp=10.0, mentions=mentions)


def test_ceremony_counts_characters_per_commit_while_holding_work() -> None:
    loud = (said("x" * (Ceremony.per_commit * 2 + 1)),)
    row, = Ceremony.derive(Talk(said=loud, committed=2))
    assert (row.characters, row.commits) == (Ceremony.per_commit * 2 + 1, 2)
    assert tuple(Ceremony.derive(Talk(said=loud, committed=3))) == ()
    assert tuple(Ceremony.derive(Talk(said=loud, committed=0, holding=False))) == ()
