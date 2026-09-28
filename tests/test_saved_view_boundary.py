"""Saved files, adapter JSON and predicates share declared field ownership."""

from dataclasses import dataclass

import pytest

from agent_comms.channels import (
    AllOfMatch,
    AnyOfMatch,
    SavedView,
    ViewKind,
    ViewMatch,
    ViewPredicate,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.tools import tool_catalog


@pytest.mark.parametrize(
    "required,observed,any_of,all_of",
    [
        ({"api", "ui"}, {"api"}, True, False),
        ({"api", "ui"}, {"api", "ui", "docs"}, True, True),
        ({"api", "ui"}, {"docs"}, False, False),
        ({"api"}, set(), False, False),
    ],
)
def test_predicate_relation_owns_matching(required, observed, any_of, all_of):
    assert ViewPredicate(AnyOfMatch, frozenset(required)).matches(frozenset(observed)) is any_of
    assert ViewPredicate(AllOfMatch, frozenset(required)).matches(frozenset(observed)) is all_of


def test_match_declarations_own_names_and_tool_choices():
    assert ViewMatch.names() == ("any_of", "all_of")
    declaration = next(tool for tool in tool_catalog() if tool["name"] == "comms_set_view")
    assert declaration["parameters"]["properties"]["match"]["enum"] == ["any_of", "all_of"]


def test_added_predicate_owner_and_field_need_no_serializer_or_dispatch(monkeypatch):
    monkeypatch.setattr(ViewMatch, "__registry__", dict(ViewMatch.__registry__))

    class DisjointMatch(ViewMatch):
        @staticmethod
        def matches(required: frozenset[str], observed: frozenset[str]) -> bool:
            return required.isdisjoint(observed)

    @dataclass(frozen=True, slots=True)
    class LabelledView(SavedView):
        label: str = "focus"

    view = LabelledView("work", ViewKind.ACTIVITY, ViewPredicate(DisjointMatch, frozenset({"ui"})))
    encoded = FieldCodec.encode(view)
    assert encoded["predicate"] == {"match": "disjoint", "tags": ["ui"]}
    assert encoded["label"] == "focus"
    assert FieldCodec.project(view, "catalog") == {k: v for k, v in encoded.items() if k != "name"}
    decoded = FieldCodec.decode(LabelledView, encoded)
    assert decoded == view
    assert decoded.predicate.matches(frozenset({"api"}))
    assert not decoded.predicate.matches(frozenset({"ui"}))


@pytest.mark.parametrize(
    "predicate",
    [
        {"match": "unknown", "tags": ["api"]},
        {"match": "any_of", "tags": []},
        {"match": "any_of", "tags": "api"},
        {"match": "any_of", "tags": [True]},
        {"match": "any_of", "tags": ["api"], "expression": "ignored?"},
    ],
)
def test_invalid_filters_fail_at_codec_boundary(predicate):
    with pytest.raises((TypeError, ValueError)):
        FieldCodec.decode(ViewPredicate, predicate)
