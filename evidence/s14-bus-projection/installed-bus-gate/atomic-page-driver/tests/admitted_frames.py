"""Original admitted ANSI and resource census; measurement never drives application state."""
import json
import os
from pathlib import Path
from time import monotonic_ns

from agent_comms.field_codec import FieldCodec
from textual._compositor import CompositorUpdate
from toad.widgets.agent_response import AgentResponse
from toad.widgets.conversation import Conversation
from toad.widgets.incoming_message import IncomingMessage
from toad.widgets.outgoing_message import OutgoingMessage
from toad.widgets.transcript_history import TranscriptHistory


class AdmittedFrames:
    def _display(self, screen, renderable):
        super()._display(screen, renderable)
        if self._batch_count or screen is not self.screen or not isinstance(renderable, CompositorUpdate):
            return
        view = self.selected_session.query_one_optional(Conversation)
        frame = {'observed_ns': monotonic_ns(), 'app_resource': id(self),
                 'width': self.size.width, 'height': self.size.height,
                 'ansi': renderable.render_segments(self.console),
                 'driver': type(self._driver).__name__, 'headless': self.is_headless,
                 'selected_session': self.selected_session.id}
        if view is not None:
            viewport = view.window.scrollable_content_region
            visible = screen._compositor.visible_widgets
            frame['viewport'] = tuple(viewport)
            frame['answers'] = [
                {'resource': id(body), 'source': body.source,
                 'region': tuple(body.region), 'visible': body in visible,
                 'in_viewport': body.region.overlaps(viewport),
                 'claim': type(body.commit_claim).__name__,
                 'ancestors': [{'resource': id(parent), 'kind': type(parent).__name__}
                               for parent in body.ancestors]}
                for body in view.contents.query(AgentResponse)]
            frame['wire_rows'] = [
                {'resource': id(body), 'reference': FieldCodec.encode(body.message_reference),
                 'region': tuple(body.region), 'visible': body in visible,
                 'in_viewport': body.region.overlaps(viewport)}
                for body in view.contents.query('IncomingMessage, OutgoingMessage')]
            frame['histories'] = [
                {'resource': id(history), 'registered': history in view.window.histories,
                 'cursor': FieldCodec.encode(history.committed_cursor),
                 'source_state': type(history.state).__name__}
                for history in view.contents.query(TranscriptHistory)]
        with (Path(os.environ['L0A_EVIDENCE']) / 'admitted-frames.jsonl').open('a') as output:
            output.write(json.dumps(frame) + '\n')


def review(path):
    # Borrow only the existing review dependency, after production imports are
    # resolved in the selected immutable runtime. No packages are installed.
    import sys
    sys.path.append('/home/ts/wt/toad-cleanup-tc2-sdk-20260929/.venv/lib/python3.14/site-packages')
    import pyte
    terminals = {}
    frames = 0
    painted = set()
    failures = []
    expected = ('RECIPIENT_RESPONSE_PROOF', 'SENDER_SENT_CONFIRMATION',
                'CONTROLLED_RETURN_0', 'CONTROLLED_RETURN_1', 'CONTROLLED_RETURN_2')
    for line in path.read_text().splitlines():
        frame = json.loads(line)
        frames += 1
        resource = frame['app_resource']
        if resource not in terminals:
            terminal = pyte.Screen(frame['width'], frame['height'])
            terminals[resource] = (terminal, pyte.Stream(terminal))
        terminal, stream = terminals[resource]
        terminal.resize(lines=frame['height'], columns=frame['width'])
        stream.feed(frame['ansi'])
        raster = [line.strip(' │▌▊▍▎▏┃>›') for line in terminal.display]
        for body in expected:
            count = raster.count(body)
            if count:
                painted.add(body)
            if count > 1:
                failures.append({'frame': frames, 'source': body, 'copies': count,
                                 'original_frame': frame, 'original_terminal': terminal.display})
    result = {'original_frames': frames, 'apps': len(terminals),
              'painted_answer_bodies': sorted(painted), 'duplicate_frames': failures,
              'boundary': 'Original admitted incremental ANSI replayed through existing pyte; exact fixture answer lines, never prompt substring dedup'}
    path.with_name('admitted-frame-review.json').write_text(json.dumps(result, indent=2) + '\n')
    assert frames and not failures, 'Original admitted terminal frames contain duplicate answer bodies'
    assert painted == set(expected), 'An expected original native answer never appeared in the admitted terminal raster'
    return result
