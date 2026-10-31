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


def source(key, revision, names):
    return SimpleNamespace(key=key, snapshot_identity=(key, revision), provenance=names)


def test_each_snapshot_is_resolved_once_and_snapshots_stay_distinct():
    earlier, later = RecordedNames("old-name"), RecordedNames("new-name")
    references = DisplayReferences("/root", {})
    from_earlier = references.for_source(source("root", 1, earlier))
    assert references.for_source(source("root", 1, earlier)) is from_earlier
    assert earlier.walks == 1
    from_later = references.for_source(source("root", 2, later))
    assert set(from_earlier.targets) == {"old-name"} and set(from_later.targets) == {"new-name"}
