"""Declared names link where they stand alone, longest first, with an optional @."""

from agent_comms.display_references import DisplayReferences


def spans(text: str) -> list[str]:
    targets = {name: name for name in ("nra", "nra-fork", "code")}
    return [text[match.start:match.end] for match in DisplayReferences("/root", targets).find(text)]


def test_a_longer_name_wins_and_at_is_part_of_the_link():
    assert spans("ask @nra-fork then nra.") == ["@nra-fork", "nra"]


def test_names_inside_other_words_paths_and_dotted_names_do_not_link():
    assert spans("xnra nra-other src/nra nra/x nra.py nra:1 #code") == []


def test_no_targets_finds_nothing():
    assert DisplayReferences("/root", {}).find("nra") == ()
