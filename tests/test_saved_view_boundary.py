"""Saved files, adapter JSON and predicates share declared field ownership."""

import json
from dataclasses import dataclass

import pytest

from agent_comms import (
    AllOfMatch,
    AnyOfMatch,
    SavedView,
    ViewKind,
    ViewMatch,
    ViewPredicate,
    invoke_tool,
)
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.tools import tool_catalog


@pytest.mark.parametrize("kind", ["participants", "activity"])
@pytest.mark.parametrize("match,owner", [("any_of", AnyOfMatch), ("all_of", AllOfMatch)])
def test_existing_saved_file_reopens_and_tools_keep_exact_format(tmp_path, kind, match, owner):
    comms = wire(tmp_path)
    comms.channels.create_tag("api")
    comms.channels.create_tag("ui")
    stored = {
        "kind": kind,
        "predicate": {"match": match, "tags": ["api", "ui"]},
        "created_at": 123.5,
    }
    path = comms.channels.catalog.saved_views_path
    path.write_text(json.dumps({"views": {"work": stored}}))
    before = path.read_bytes()
    reopened = wire(tmp_path)
    view = reopened.channels.catalog.saved_views()["work"]
    assert view.predicate.match is owner
    assert FieldCodec.encode(view) == {"name": "work", **stored}
    assert path.read_bytes() == before  # A read is not an implicit migration.
    assert invoke_tool(reopened, "comms_channels", {})["views"] == [{"name": "work", **stored}]
    reopened.channels.create_tag("unrelated")  # Exercise the actual catalog writer.
    assert json.loads(path.read_text()) == {"views": {"work": stored}}
    assert wire(tmp_path).channels.catalog.saved_views() == {"work": view}


def test_missing_saved_timestamp_stays_unknown_across_read_and_write(tmp_path):
    comms = wire(tmp_path)
    comms.channels.create_tag("api")
    path = comms.channels.catalog.saved_views_path
    path.write_text(
        json.dumps(
            {
                "views": {
                    "undated": {
                        "kind": "activity",
                        "predicate": {"match": "any_of", "tags": ["api"]},
                    }
                }
            }
        )
    )
    view = wire(tmp_path).channels.catalog.saved_views()["undated"]
    assert view.created_at == 0.0
    comms.channels.set_saved_view(view)
    assert wire(tmp_path).channels.catalog.saved_views()["undated"].created_at == 0.0


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


@pytest.mark.parametrize("extra", [{"extension": "discard?"}, {"name": "other"}])
def test_catalog_rejects_unknown_fields_or_a_second_name_authority(tmp_path, extra):
    comms = wire(tmp_path)
    row = {"kind": "participants", "predicate": {"match": "any_of", "tags": ["api"]}, **extra}
    path = comms.channels.catalog.saved_views_path
    path.write_text(json.dumps({"views": {"work": row}}))
    before = path.read_bytes()
    with pytest.raises((TypeError, ValueError)):
        comms.channels.catalog.saved_views()
    assert path.read_bytes() == before
