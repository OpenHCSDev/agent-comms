"""Conversion declarations, not the publisher, own writer admission."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from tools.cutover.carry import NoConversion, PendingConversion


def test_no_conversion_switches_before_releasing_its_hold(tmp_path):
    events = []
    publication = SimpleNamespace(
        selected_route=lambda root: nullcontext(),
        published=lambda: False,
        publish_links=lambda: events.append('pointer'),
        release_if_safe=lambda *args: events.append('release'),
    )
    comms = SimpleNamespace(root=tmp_path, owners=SimpleNamespace(maintenance=object()),
                            registry=SimpleNamespace(snapshot=lambda: object()))
    NoConversion(tmp_path).publish(publication, comms, SimpleNamespace(owners=()),
                                    object(), nullcontext(), 0)
    assert events == ['pointer', 'release']


def test_pending_conversion_ignores_a_selected_process_that_stopped(tmp_path):
    events = []
    stopped = SimpleNamespace(process=SimpleNamespace(alive=lambda: False))
    publication = SimpleNamespace(publish_stopped=lambda *args: events.append('stopped'))
    comms = SimpleNamespace(registry=SimpleNamespace(snapshot=lambda: object()))
    PendingConversion(None, (), (), None).publish(
        publication, comms, SimpleNamespace(owners=(stopped,)), object(), nullcontext(), 0)
    assert events == ['stopped']
