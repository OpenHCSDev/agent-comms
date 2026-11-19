"""Full captured values, recipient identity, partial publication and atomic replacement."""

import json
from dataclasses import dataclass, replace
from typing import Annotated

import pytest
from cutover.carry_shared_team_frames import FlatTeamFrameFile, SharedTeamFrames

from agent_comms.coordination.service_passes import BoardUnavailable
from agent_comms.nominal.field_codec import (
    FieldCodec,
    MalformedRecord,
    OrderId,
    RecordMember,
    WireInline,
)
from agent_comms.nominal.shared_json import JsonArray, JsonCapture, JsonLiteral, SharedJson
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.storage.locked_store import LiveFilesRequest
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.task_attachments.team_pings import Ping, RulePing, TeamDecisions, TeamUpdateNotice
from agent_comms.team.standing_rules import ReviewTimerUp
from agent_comms.team.team_hud import (
    AllTeamBoards,
    CodexUsage,
    NamedTeamBoards,
    TeamBoardSelection,
    TeamFrame,
    TeamFramesDocument,
    TeamHud,
    TeamHudRecord,
    TeamPlan,
    UnreadableUsage,
)
from agent_comms.team.vibe_check import OverloadDegradation


def huds():
    owners = tuple(RecordedThreadIncarnation(name, float(i + 1))
                   for i, name in enumerate(("first", "second", "third")))
    ping = RulePing.capture(ReviewTimerUp(problem="P1", holder=owners[0],
        order=OrderId("123456789abc"), since=RecordedEventTime(10.0)))
    changed = replace(ping, text="A different original text", since=RecordedEventTime(11.0))
    assert ping == changed  # Logical alarm identity deliberately ignores these fields.
    pings = (ping, changed, ping)
    packets = (TeamDecisions(owners[0], pings, (changed,), (ping,)),
               TeamUpdateNotice(owners[1], pings),
               TeamDecisions(owners[2], pings, (ping,), (changed,), not_applied=("kept",)))
    first = TeamHud(teammate=owners[0], units=(), forks=(),
        usage=CodexUsage(UnreadableUsage("not requested")),
        load=OverloadDegradation.from_orders((), 0, 0), packets=(packets[0],), open_pings=pings)
    return tuple(replace(first, teammate=owner, packets=(packet,))
                 for owner, packet in zip(owners, packets, strict=True))


def values(items):
    # Full typed wire data, never TeamHud/Ping logical equality.
    return FieldCodec.encode(items, tuple[TeamHudRecord, ...])


def test_one_snapshot_preserves_every_recipient_and_encodes_each_ping_once(tmp_path, monkeypatch):
    original = huds()
    expected = values(original)
    calls = []
    encode = Ping.encode_shared.__func__
    def counted(cls, value, encoding):
        calls.append(id(value))
        return encode(cls, value, encoding)
    monkeypatch.setattr(Ping, "encode_shared", classmethod(counted))
    TeamFrame.replace_all(tmp_path, TeamPlan(), original)
    assert len(calls) == len(set(calls)) == 2
    assert len(TeamFrame.matching_in(tmp_path)) == 1
    assert values(TeamFrame.read_all(tmp_path)) == expected
    for item in original:
        assert values((TeamFrame.at(tmp_path, item.teammate).read(),)) == values((item,))
    assert original[0].open_pings[0].text != original[0].open_pings[1].text
    # Public encoding is still detached from the Ping's private cached representation.
    public = Ping.encode(original[0].open_pings[0])
    public["text"] = "caller edit"
    assert Ping.encode(original[0].open_pings[0])["text"] != "caller edit"
    captured = FieldCodec.encode_shared(original, tuple[TeamHudRecord, ...])
    captured[0]["open_pings"][0]["subjects"].clear()
    captured[0]["open_pings"][0]["text"] = "another caller edit"
    assert Ping.encode(original[0].open_pings[0])["text"] != "another caller edit"
    assert values(TeamFrame.read_all(tmp_path)) == expected


@pytest.mark.parametrize("selection, names", [
    (AllTeamBoards(), ("first", "second", "third")),
    (NamedTeamBoards(frozenset(("second",))), ("second",)),
    (NamedTeamBoards(frozenset(("first", "third"))), ("first", "third")),
    (NamedTeamBoards(frozenset(("missing",))), ()),
    (NamedTeamBoards(frozenset()), ()),
])
def test_selected_frames_decode_only_requested_recipients(tmp_path, monkeypatch, selection, names):
    original = huds()
    TeamFrame.replace_all(tmp_path, TeamPlan(), original)
    selection = FieldCodec.decode(TeamBoardSelection, FieldCodec.encode(selection, TeamBoardSelection))
    decoded = []
    decode = FieldCodec.decode

    def counted(annotation, raw):
        if annotation is TeamHud:
            decoded.append(raw)
        return decode(annotation, raw)

    monkeypatch.setattr(FieldCodec, "decode", counted)
    selected = TeamFrame.read_all(tmp_path, selection)
    assert tuple(hud.teammate.name for hud in selected) == names
    assert len(decoded) == len(names)
    assert values(selected) == values(tuple(hud for hud in original if hud.teammate.name in names))


def test_combined_board_selection_keeps_every_visible_page_and_full_board():
    first, second = (TeamBoardSelection.named(name) for name in ("first", "second"))
    combined = first.combined(second)
    assert combined == NamedTeamBoards(frozenset(("first", "second")))
    assert combined.combined(first) == combined
    assert combined.combined(AllTeamBoards()) == AllTeamBoards()
    assert AllTeamBoards().combined(combined) == AllTeamBoards()
    assert TeamBoardSelection.named("") == AllTeamBoards()


def test_partial_and_full_passes_keep_their_actual_membership_and_old_cuts(tmp_path):
    original = huds()
    TeamFrame.replace_all(tmp_path, TeamPlan(), original)
    path = TeamFrame.path_in(tmp_path)
    before = path.read_bytes()
    assert TeamFrame.write_all(tmp_path, ()) == frozenset()
    assert path.read_bytes() == before
    changed = replace(original[0], packets=(), open_pings=())
    TeamFrame.write_all(tmp_path, (changed,))
    assert values(TeamFrame.read_all(tmp_path)) == values((changed, *original[1:]))
    TeamFrame.replace_all(tmp_path, TeamPlan(), (changed,))
    assert values(TeamFrame.read_all(tmp_path)) == values((changed,))
    with pytest.raises(BoardUnavailable):
        TeamFrame.at(tmp_path, original[1].teammate).read()
    replacement = replace(changed, teammate=replace(changed.teammate, created_at=9.0))
    TeamFrame.replace_all(tmp_path, TeamPlan(), (replacement,))
    with pytest.raises(BoardUnavailable):
        TeamFrame.at(tmp_path, changed.teammate).read()


def test_a_failed_atomic_publication_keeps_the_previous_complete_snapshot(tmp_path, monkeypatch):
    original = huds()
    TeamFrame.replace_all(tmp_path, TeamPlan(), original)
    before = TeamFrame.path_in(tmp_path).read_bytes()
    def failed(self, text):
        raise OSError("staging failed")
    monkeypatch.setattr(TeamFramesDocument, "_write_unlocked", failed)
    with pytest.raises(OSError, match="staging failed"):
        TeamFrame.replace_all(tmp_path, TeamPlan(), original[:1])
    assert TeamFrame.path_in(tmp_path).read_bytes() == before
    assert values(TeamFrame.read_all(tmp_path)) == values(original)


def test_flat_frames_carry_once_without_advancing_an_existing_recipient(tmp_path):
    original = huds()
    for item in original:
        path = FlatTeamFrameFile.path_in(tmp_path, lookup=str(item.teammate.require_recorded().lookup))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(FieldCodec.encode(item, TeamHudRecord)))
    step = SharedTeamFrames(LiveFilesRequest(tmp_path), tmp_path / "spill")
    assert TeamFrame.path_in(tmp_path) in step.input_files()
    assert step.carry(apply=False).changed == 3
    assert not TeamFrame.path_in(tmp_path).exists()
    changed = replace(original[0], packets=(), open_pings=())
    TeamFrame.replace_all(tmp_path, TeamPlan(), (changed,))
    assert step.carry(apply=True).changed == 3
    assert values(TeamFrame.read_all(tmp_path)) == values((changed, *original[1:]))
    assert not FlatTeamFrameFile.matching_in(tmp_path)
    assert step.carry(apply=False).changed == 0


@pytest.mark.parametrize("document", [SharedJson((JsonArray((0,)),), (0,)),
    SharedJson((JsonLiteral(1),), (-1,)), SharedJson((JsonLiteral(1),), (1,))])
def test_invalid_graph_references_fail_at_the_typed_boundary(document):
    with pytest.raises(MalformedRecord):
        document.decode(tuple[int, ...])


def test_last_recipient_cut_and_duplicate_stored_roots(tmp_path):
    original = huds()
    latest = replace(original[0], packets=(), open_pings=())
    TeamFramesDocument(TeamFrame.path_in(tmp_path)).publish((*original, latest))
    assert values(TeamFrame.read_all(tmp_path)) == values((latest, *original[1:]))
    invalid = SharedJson.of((original[0], latest), tuple[TeamHudRecord, ...])
    TeamFrame.path_in(tmp_path).write_text(FieldCodec.compact(FieldCodec.encode(invalid)))
    with pytest.raises(MalformedRecord, match="repeats a recipient"):
        TeamFrame.read_all(tmp_path)


def test_shared_capture_retains_inline_fields_fixed_tuples_sets_and_custom_values():
    @dataclass(frozen=True)
    class Inline(RecordMember):
        label: str
        count: int

    @dataclass(frozen=True)
    class Record:
        detail: Annotated[Inline, WireInline()]
        pair: tuple[int, str]
        members: frozenset[int]
        mapping: dict[str, Inline]

    detail = Inline("kept", 4)
    records = (Record(detail, (2, "tail"), frozenset((3, 1)), {"original": detail}),) * 3
    encoded = FieldCodec.encode(records, tuple[Record, ...])
    document = SharedJson.of(records, tuple[Record, ...])
    decoded = document.decode(tuple[Record, ...])
    assert FieldCodec.encode(decoded, tuple[Record, ...]) == encoded
    # Both the raw snapshot and its native records are owned by this read.
    decoded[0].mapping.clear()
    assert document.decode(tuple[Record, ...])[0].mapping


def test_shared_json_keeps_scalar_types_signed_zero_and_inline_custom_shapes():
    data = (True, 1, 1.0, -0.0, 0.0, [True, 1])
    document = SharedJson.of(data, tuple[object, ...])
    assert FieldCodec.compact(document.decode(tuple[object, ...])) == FieldCodec.compact(data)
    cycle = []
    cycle.append(cycle)
    with pytest.raises(MalformedRecord, match="cycle"):
        JsonCapture().capture(cycle)


def test_one_recipient_decodes_only_its_board_and_checks_every_identity(tmp_path, monkeypatch):
    original = huds()
    TeamFrame.replace_all(tmp_path, TeamPlan(), original)
    decoded = []
    decode = FieldCodec.decode

    def counted(target, data):
        if target is TeamHud:
            decoded.append(target)
        return decode(target, data)

    monkeypatch.setattr(FieldCodec, "decode", counted)
    selected = TeamFrame.at(tmp_path, original[1].teammate).read()
    assert values((selected,)) == values((original[1],))
    assert decoded == [TeamHud]
    invalid = SharedJson.of((original[0], original[0]), tuple[TeamHudRecord, ...])
    TeamFrame.path_in(tmp_path).write_text(FieldCodec.compact(FieldCodec.encode(invalid)))
    with pytest.raises(MalformedRecord, match="repeats a recipient"):
        TeamFrame.at(tmp_path, original[0].teammate).read()
