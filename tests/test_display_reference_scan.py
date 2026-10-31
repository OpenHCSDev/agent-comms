"""Declared names link where they stand alone, longest first, with an optional @."""

from types import SimpleNamespace

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


class RecordedNames:
    """Counts how often a source's names are walked; history bodies share one source."""

    def __init__(self, *names):
        self.names, self.walks = names, 0

    def named_members(self):
        self.walks += 1
        return tuple((name, SimpleNamespace(incarnation=SimpleNamespace(name=name))) for name in self.names)


def test_each_source_is_resolved_once_and_sources_stay_distinct():
    first, second = RecordedNames("old-name"), RecordedNames("other-name")
    references = DisplayReferences("/root", {})
    from_first = references.for_source(SimpleNamespace(key="first", provenance=first))
    assert references.for_source(SimpleNamespace(key="first", provenance=first)) is from_first
    assert first.walks == 1
    from_second = references.for_source(SimpleNamespace(key="second", provenance=second))
    assert set(from_first.targets) == {"old-name"} and set(from_second.targets) == {"other-name"}
