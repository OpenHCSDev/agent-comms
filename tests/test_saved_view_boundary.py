"""Saved files, adapter JSON and predicates share declared field ownership."""


import pytest

from agent_comms.channels import (
    AllOfMatch,
    AnyOfMatch,
    ViewMatch,
    ViewPredicate,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.native_tools import NativeRequestTools


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
    declaration = next(tool for tool in NativeRequestTools.catalog() if tool["name"] == "comms_set_view")
    assert declaration["parameters"]["properties"]["match"]["enum"] == ["any_of", "all_of"]


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
