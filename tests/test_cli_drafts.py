"""The shared owner reconstructs prefixes, not the CLI decoders or the database."""
import pytest

from agent_comms.native_tool_call import ToolCallId
from agent_comms.turn_item import NativeItemId, NativeTurnId
from agent_comms.turn_phase import ModelWaitPhase, ToolRunningPhase
from agent_comms.runtime_updates import (
    ActiveItem, AllTurnsEnded, AppendDraftItem, CompletedItem, DraftAccumulator, FinishedItem,
    FinishedTurn, ParagraphDraft, StartedItem, TextDraft, ToolDraft, TurnEnded,
)
from agent_comms.recorded_time import EventTime, RecordedEventTime, UnrecordedEventTime
from agent_comms.transcript_events import AssistantTranscript, ThinkingTranscript
from agent_comms.transcript_sources import NoDraft
from agent_comms.tool_calls import ToolCall
from test_codex_items import _output


def start(
    turn: NativeTurnId, item: str, text: str = '', accumulator: type[TextDraft] = TextDraft,
) -> StartedItem:
    return StartedItem(
        turn,
        NativeItemId(item),
        ModelWaitPhase("Replying"),
        accumulator.from_parts(
            AssistantTranscript, (text,), RecordedEventTime(1.0), NativeItemId(item)
        ),
    )


def test_batched_deltas_materialize_only_the_surviving_view(tmp_path):
    t_turn_id = NativeTurnId('t')
    class CountedDraft(TextDraft):
        reads = 0

        @property
        def events(self):
            self.reads += 1
            return super().events

    output = _output(tmp_path)
    begun = start(t_turn_id, 'a', accumulator=CountedDraft)
    output.publish_batch(
        [begun, *(AppendDraftItem(t_turn_id, NativeItemId("a"), "x") for _ in range(1000))]
    )
    assert begun.accumulator.reads == 1
    draft = output.transcript.current_draft(output.transcript.root, output.transcript.incarnation)
    assert draft.events == (
        AssistantTranscript("x" * 1000, timestamp=1.0, position=NativeItemId("a")),
    )


def test_prefix_required_and_completion_rejects_overlapping_start_and_suffix(tmp_path):
    t_turn_id = NativeTurnId('t')
    output = _output(tmp_path)
    activity = ActiveItem(t_turn_id, NativeItemId('a'), ModelWaitPhase('Replying'))
    assert activity.accumulate(output) == (activity,)
    output.publish_batch([AppendDraftItem(t_turn_id, NativeItemId('a'), 'suffix')])
    assert output.transcript.current_draft(output.transcript.root, output.transcript.incarnation) == NoDraft()
    assert not output.drafts
    output.publish_batch(
        [start(t_turn_id, "a"), AppendDraftItem(t_turn_id, NativeItemId("a"), "Hello")]
    )
    output.publish_batch([
        CompletedItem(t_turn_id, NativeItemId('a'), (AssistantTranscript('Hello!', timestamp=9.0,
            position=NativeItemId('a')),)),
        start(t_turn_id, 'a', 'stale'), AppendDraftItem(t_turn_id, NativeItemId('a'), ' late'),
    ])
    assert output.transcript.current_draft(output.transcript.root, output.transcript.incarnation) == NoDraft()
    with output.transcript.reading() as snapshot:
        row, = snapshot.forward(0, snapshot.cut().offset)
        assert row.events == (
            AssistantTranscript("Hello!", timestamp=9.0, position=NativeItemId("a")),
        )
    assert (
        ActiveItem(t_turn_id, NativeItemId("a"), ModelWaitPhase("stale")).accumulate(output) == ()
    )


def test_saved_completion_survives_initial_idle_snapshot_until_ordered_end(tmp_path):
    old_turn_id = NativeTurnId('old')
    new_turn_id = NativeTurnId('new')
    output = _output(tmp_path)
    output.publish_batch(
        [
            CompletedItem(
                old_turn_id,
                NativeItemId("a"),
                (AssistantTranscript("saved", timestamp=9.0, position=NativeItemId("a")),),
            ),
            TurnEnded(),
            start(old_turn_id, "a", "overlapping snapshot"),
            AppendDraftItem(old_turn_id, NativeItemId("a"), "late"),
        ]
    )
    assert output.transcript.current_draft(output.transcript.root, output.transcript.incarnation) == NoDraft()
    with output.transcript.reading() as snapshot:
        row, = snapshot.forward(0, snapshot.cut().offset)
        assert row.events == (
            AssistantTranscript("saved", timestamp=9.0, position=NativeItemId("a")),
        )
    output.publish_batch([AllTurnsEnded(), start(new_turn_id, 'a', 'new identity')])
    assert output.transcript.current_draft(output.transcript.root, output.transcript.incarnation).turn_id == 'new'


def test_ending_one_turn_preserves_other_live_prefixes(tmp_path):
    one_turn_id = NativeTurnId('one')
    two_turn_id = NativeTurnId('two')
    output = _output(tmp_path)
    output.publish_batch(
        [
            start(one_turn_id, "same-item", "one"),
            start(two_turn_id, "same-item", "two"),
            FinishedTurn(one_turn_id),
            AppendDraftItem(two_turn_id, NativeItemId("same-item"), " continued"),
        ]
    )
    assert output.transcript.current_draft(output.transcript.root, output.transcript.incarnation).events[0].text == 'two continued'
    assert AppendDraftItem(one_turn_id, NativeItemId('same-item'), 'late').accumulate(output) == ()
    output.publish_batch(
        [AllTurnsEnded(), AppendDraftItem(two_turn_id, NativeItemId("same-item"), "late")]
    )
    assert output.transcript.current_draft(output.transcript.root, output.transcript.incarnation) == NoDraft()


def test_paragraph_identity_not_arrival_order_owns_summary_joining(tmp_path):
    t_turn_id = NativeTurnId('t')
    from agent_comms.cli_bridges.codex_protocol import ReasoningItem

    output = _output(tmp_path)
    accumulator = ParagraphDraft.from_parts(
        ThinkingTranscript, [], RecordedEventTime(2.0), NativeItemId("r")
    )
    output.publish_batch(
        [
            StartedItem(t_turn_id, NativeItemId("r"), ModelWaitPhase(), accumulator),
            AppendDraftItem(t_turn_id, NativeItemId("r"), "Two", 1),
            AppendDraftItem(t_turn_id, NativeItemId("r"), "One", 0),
            AppendDraftItem(t_turn_id, NativeItemId("r"), " plus", 1),
        ]
    )
    draft = output.transcript.current_draft(output.transcript.root, output.transcript.incarnation)
    completed = ReasoningItem(id='r', type='reasoning', summary=['One', 'Two plus'])
    assert draft.events[0].text == completed.events(2.0)[0].text == 'One\n\nTwo plus'
    assert draft.events[0].timestamp == 2.0


def test_tool_start_is_one_draft_row_then_one_completed_row(tmp_path):
    t_turn_id = NativeTurnId('t')
    output = _output(tmp_path)
    call = ToolCall(ToolCallId('call'), 'read', {'path': 'a.py'})
    start_event = call.started(NativeItemId('call'), 1.0)
    output.publish_batch([StartedItem(
        t_turn_id, NativeItemId('call'), ToolRunningPhase(tools=((ToolCallId('call'), 'Read a.py'),)),
        ToolDraft(RecordedEventTime(1.0), (start_event,)))])
    draft = output.transcript.current_draft(output.transcript.root, output.transcript.incarnation)
    assert draft.events == (start_event,)
    output.publish_batch(
        [
            CompletedItem(
                t_turn_id,
                NativeItemId("call"),
                call.finished("done", True, NativeItemId("call"), 2.0),
            )
        ]
    )
    assert output.transcript.current_draft(output.transcript.root, output.transcript.incarnation) == NoDraft()
    with output.transcript.reading() as snapshot:
        row, = snapshot.forward(0, snapshot.cut().offset)
        assert row.events == call.finished('done', True, NativeItemId('call'), 2.0)


def test_empty_tool_collection_has_activity_but_no_blank_row(tmp_path):
    t_turn_id = NativeTurnId('t')
    output = _output(tmp_path)
    phase = ToolRunningPhase('Editing files')
    output.publish_batch([StartedItem(t_turn_id, NativeItemId('empty'), phase,
        ToolDraft(RecordedEventTime(1.0), ()))])
    assert output.transcript.current_draft(output.transcript.root, output.transcript.incarnation) == NoDraft()


def test_non_text_items_never_acquire_invented_text(tmp_path):
    t_turn_id = NativeTurnId('t')
    output = _output(tmp_path)
    output.publish_batch([StartedItem(t_turn_id, NativeItemId('tool'), ToolRunningPhase('Run'),
        DraftAccumulator(RecordedEventTime(1.0)))])
    with pytest.raises(ValueError):
        AppendDraftItem(t_turn_id, NativeItemId('tool'), 'not a reply').accumulate(output)
    assert output.transcript.current_draft(output.transcript.root, output.transcript.incarnation) == NoDraft()


@pytest.mark.parametrize('started_at', [
    RecordedEventTime(0.0), RecordedEventTime(1.0), UnrecordedEventTime(),
])
def test_completion_keeps_its_original_clock_for_every_start_time(tmp_path, started_at):
    t_turn_id = NativeTurnId('t')
    output = _output(tmp_path)
    output.publish_batch([
        StartedItem(t_turn_id, NativeItemId('a'), ModelWaitPhase(), DraftAccumulator(started_at)),
        CompletedItem(t_turn_id, NativeItemId('a'), (AssistantTranscript('finished', timestamp=9.0,
            position=NativeItemId('a')),)),
    ])
    with output.transcript.reading() as snapshot:
        row, = snapshot.forward(0, snapshot.cut().offset)
        assert row.events[0].timestamp == 9.0


@pytest.mark.parametrize('wire, state, expected', [
    ({}, UnrecordedEventTime, 2.0),
    ({'startedAtMs': None}, UnrecordedEventTime, 2.0),
    ({'startedAtMs': 0}, RecordedEventTime, 0.0),
    ({'startedAtMs': 1500}, RecordedEventTime, 1.5),
])
def test_wire_start_decoder_selects_time_state(wire, state, expected):
    from agent_comms.cli_bridges.codex_protocol import ItemEntry

    entry = ItemEntry(turnId='t', item={'type': 'agentMessage', 'id': 'a', 'text': 'epoch'},
                      completedAtMs=2000, **wire)
    assert isinstance(entry.started_at, state)
    assert entry.publication().events[0].timestamp == expected


@pytest.mark.parametrize('invalid', [None, True, '0', float('nan'), float('inf')])
def test_capture_requires_a_real_recorded_timestamp(invalid):
    with pytest.raises(ValueError):
        EventTime.capture(invalid)


@pytest.mark.parametrize('invalid', [True, '0', 1.5])
def test_wire_start_decoder_keeps_strict_integer_input(invalid):
    from pydantic import ValidationError
    from agent_comms.cli_bridges.codex_protocol import ItemEntry

    with pytest.raises(ValidationError):
        ItemEntry(turnId='t', item={'type': 'agentMessage', 'id': 'a', 'text': 'invalid'},
                  startedAtMs=invalid, completedAtMs=2000)


def test_completed_only_sources_do_not_retain_draft_state(tmp_path):
    t_turn_id = NativeTurnId('t')
    output = _output(tmp_path)
    output.publish_batch(
        [
            FinishedItem(
                t_turn_id,
                NativeItemId("a"),
                (AssistantTranscript("complete", timestamp=1.0, position=NativeItemId("a")),),
            )
        ]
    )
    assert not output.drafts
    assert output.transcript.current_draft(output.transcript.root, output.transcript.incarnation) == NoDraft()
