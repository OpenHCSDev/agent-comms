"""The shared owner reconstructs prefixes, not the CLI decoders or the database."""
import pytest

from agent_comms.activity import ActivityState
from agent_comms.cli_bridges.shared import (
    ActiveItem, AllTurnsEnded, AppendDraftItem, CompletedItem, FinishedItem,
    FinishedTurn, NonTextDraft, ParagraphDraft, StartedItem, TextDraft, TurnEnded,
)
from agent_comms.transcript_events import AssistantTranscript, ThinkingTranscript
from test_codex_items import _output


def start(turn, item, text='', accumulator=TextDraft):
    return StartedItem(turn, item, ActivityState.WORKING, 'Replying',
                       accumulator.from_event(AssistantTranscript(text, timestamp=1.0)))


def test_batched_deltas_materialize_only_the_surviving_view(tmp_path):
    class CountedDraft(TextDraft):
        reads = 0

        @property
        def events(self):
            self.reads += 1
            return super().events

    output = _output(tmp_path)
    begun = start('t', 'a', accumulator=CountedDraft)
    output.publish_batch([begun, *(AppendDraftItem('t', 'a', 'x') for _ in range(1000))])
    assert begun.accumulator.reads == 1
    draft = output.transcript.current_draft()
    assert draft.events == (AssistantTranscript('x' * 1000, timestamp=1.0),)


def test_prefix_required_and_completion_rejects_overlapping_start_and_suffix(tmp_path):
    output = _output(tmp_path)
    output.publish_batch([AppendDraftItem('t', 'a', 'suffix')])
    assert output.transcript.current_draft() is None
    output.publish_batch([start('t', 'a'), AppendDraftItem('t', 'a', 'Hello')])
    output.publish_batch([
        CompletedItem('t', 'a', (AssistantTranscript('Hello!', timestamp=9.0),)),
        start('t', 'a', 'stale'), AppendDraftItem('t', 'a', ' late'),
    ])
    assert output.transcript.current_draft() is None
    with output.transcript.reading() as snapshot:
        row, = snapshot.forward(0, snapshot.cut().offset)
        assert row.events == (AssistantTranscript('Hello!', timestamp=1.0),)
    assert ActiveItem('t', 'a', ActivityState.THINKING, 'stale').accumulate(output) == ()


def test_saved_completion_survives_initial_idle_snapshot_until_ordered_end(tmp_path):
    output = _output(tmp_path)
    output.publish_batch([
        CompletedItem('old', 'a', (AssistantTranscript('saved'),)), TurnEnded(),
        start('old', 'a', 'overlapping snapshot'), AppendDraftItem('old', 'a', 'late'),
    ])
    assert output.transcript.current_draft() is None
    output.publish_batch([AllTurnsEnded(), start('new', 'a', 'new identity')])
    assert output.transcript.current_draft().turn_id == 'new'


def test_ending_one_turn_preserves_other_live_prefixes(tmp_path):
    output = _output(tmp_path)
    output.publish_batch([start('one', 'a', 'one'), start('two', 'b', 'two'),
                          FinishedTurn('one'), AppendDraftItem('two', 'b', ' continued')])
    assert output.transcript.current_draft().events[0].text == 'two continued'
    assert AppendDraftItem('one', 'a', 'late').accumulate(output) == ()
    output.publish_batch([AllTurnsEnded(), AppendDraftItem('two', 'b', 'late')])
    assert output.transcript.current_draft() is None


def test_paragraph_identity_not_arrival_order_owns_summary_joining(tmp_path):
    from agent_comms.cli_bridges.codex_protocol import ReasoningItem

    output = _output(tmp_path)
    accumulator = ParagraphDraft.from_parts(ThinkingTranscript('', timestamp=2.0), [])
    output.publish_batch([
        StartedItem('t', 'r', ActivityState.THINKING, '', accumulator),
        AppendDraftItem('t', 'r', 'Two', 1), AppendDraftItem('t', 'r', 'One', 0),
        AppendDraftItem('t', 'r', ' plus', 1),
    ])
    draft = output.transcript.current_draft()
    completed = ReasoningItem(id='r', type='reasoning', summary=['One', 'Two plus'])
    assert draft.events[0].text == completed.events()[0].text == 'One\n\nTwo plus'
    assert draft.events[0].timestamp == 2.0


def test_non_text_items_never_acquire_invented_text(tmp_path):
    output = _output(tmp_path)
    output.publish_batch([StartedItem('t', 'tool', ActivityState.WORKING, 'Run', NonTextDraft(1.0))])
    with pytest.raises(ValueError, match='non-text item'):
        AppendDraftItem('t', 'tool', 'not a reply').accumulate(output)
    assert output.transcript.current_draft() is None


def test_completed_only_sources_do_not_retain_draft_state(tmp_path):
    output = _output(tmp_path)
    output.publish_batch([FinishedItem('t', 'a', (AssistantTranscript('complete'),))])
    assert not output.drafts
    assert output.transcript.current_draft() is None
