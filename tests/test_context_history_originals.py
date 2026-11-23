"""A turn of a recorded context finds the text it was before anything was cut from it."""

from __future__ import annotations

from agent_comms.execution.context_history import ContextHistory


def test_the_original_of_a_turn_is_found_from_the_turn_as_it_was_sent(tmp_path):
    history = ContextHistory(tmp_path / "context.git", "session-1")
    sent, original = "head\n[... 3 characters cut ...]\ntail é", "head\nmiddle\ntail é"
    history.keep_original(sent, original)
    history.keep_original(sent, original)  # kept already: not an error
    assert history.original_of(sent) == original
    assert history.original_path(sent).parent == tmp_path / "context.git.originals" / "session-1"


def test_a_turn_nothing_was_kept_for_is_its_own_original(tmp_path):
    history = ContextHistory(tmp_path / "context.git", "session-1")
    assert history.original_of("whole") == "whole"
    history.keep_original("", "")  # an empty result is kept like any other
    assert history.original_of("") == ""
