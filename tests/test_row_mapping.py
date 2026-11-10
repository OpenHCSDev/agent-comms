"""Row snapshots retain values and compare only the edits made to related roots."""

from collections.abc import MutableMapping
from dataclasses import dataclass

import pytest

from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord
from agent_comms.storage.row_mapping import RowMapping


@dataclass(frozen=True)
class ObservedValue:
    number: int
    comparisons: list[tuple[int, int]]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ObservedValue):
            return NotImplemented
        self.comparisons.append((self.number, other.number))
        return self.number == other.number


@pytest.mark.parametrize('size', (400, 3000))
def test_copy_and_freeze_compare_only_the_changed_row(size):
    comparisons: list[tuple[int, int]] = []
    original = RowMapping({str(i): ObservedValue(i, comparisons) for i in range(size)})
    frozen = original.freeze()
    edit = original.copy()
    edit['20'] = ObservedValue(-1, comparisons)
    comparisons.clear()
    assert edit.differing(frozen) == frozenset({'20'})
    assert comparisons == [(-1, 20)]
    assert frozen['20'].number == 20
    assert original['20'].number == 20
    assert len(edit) == size


def test_related_edits_include_additions_removals_and_sibling_changes():
    original = RowMapping({'a': 1, 'b': 2})
    first, second = original.copy(), original.copy()
    first['a'] = 3
    second['c'] = 4
    del second['b']
    assert first.differing(second) == frozenset({'a', 'b', 'c'})
    assert second.freeze().differing(original.freeze()) == frozenset({'b', 'c'})
    assert first.differing(RowMapping({'a': 3, 'b': 2})) == frozenset()


def test_iteration_keeps_insertion_order_across_edits():
    rows = RowMapping({'a': 1, 'b': 2})
    rows['a'] = 3
    assert list(rows) == ['a', 'b']
    del rows['a']
    rows['a'] = 4
    assert list(rows) == ['b', 'a']
    rows.clear()
    assert not rows
    with pytest.raises(KeyError):
        rows.popitem()
    rows.update({'x': 5})
    assert rows.popitem() == ('x', 5)


def test_changed_keys_keep_new_rows_in_insertion_order():
    original = RowMapping({'kept': 0})
    edit = original.copy()
    edit.update({'z': 1, 'a': 2, 'm': 3})
    assert edit.changed_in_order(original) == ('z', 'a', 'm')


def test_frozen_roots_reject_mutation_but_can_start_a_new_edit():
    frozen = RowMapping({'a': 1}).freeze()
    with pytest.raises(TypeError, match='read-only'):
        frozen['a'] = 2
    with pytest.raises(TypeError, match='read-only'):
        del frozen['a']
    with pytest.raises(TypeError, match='read-only'):
        frozen.clear()
    edit = frozen.copy()
    edit['a'] = 2
    assert frozen == {'a': 1}
    assert edit == {'a': 2}


@dataclass
class MappingRecord:
    rows: MutableMapping[str, int]


def test_the_existing_codec_handles_owned_mapping_values_without_a_second_wire_format():
    value = MappingRecord(RowMapping({'a': 1}))
    assert FieldCodec.encode(value) == {'rows': {'a': 1}}
    assert FieldCodec.encode(value.rows, MutableMapping[str, int]) == {'a': 1}
    assert FieldCodec.decode(MappingRecord, {'rows': {'a': 1}}) == value
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(MutableMapping[str, int], value.rows)
