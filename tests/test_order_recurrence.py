"""Recurring defects require factoring; changed requirements keep their real edit scope."""

from dataclasses import replace

import pytest

from agent_comms.errors import TodoError
from agent_comms.nominal.field_codec import FieldCodec, GoalId, MalformedRecord, OrderId
from agent_comms.planning.order_words import PlainText
from agent_comms.planning.orders import (
    CheckedDelivery,
    DeliverTodo,
    OperationDelivery,
    OrderScope,
    RequirementChangeScope,
)
from agent_comms.projects.change_report import FileLine
from agent_comms.projects.slop import DeclarationDerivation, Derivation
from agent_comms.storage.recorded_time import RecordedEventTime
from test_todos import order, thread


@pytest.fixture
def source(tmp_path):
    (tmp_path / 'rules.py').write_text(
        'LIMIT = 1\n'
        '\n'
        'class First:\n'
        '    def check(self):\n'
        '        return 1\n'
        '\n'
        'class Second:\n'
        '    def check(self):\n'
        '        return 2\n'
    )
    (tmp_path / 'other.py').write_text('class Owner:\n    pass\n')
    return tmp_path


@pytest.mark.parametrize('mechanism', Derivation.members_with(DeclarationDerivation))
def test_declaration_derivations_require_another_declaration(mechanism, source):
    where = FileLine('rules.py', 9)
    assert not mechanism.recurrence_refusal(FileLine('rules.py', 3), where, source)
    assert not mechanism.recurrence_refusal(FileLine('other.py', 1), where, source)
    assert mechanism.recurrence_refusal(FileLine('rules.py', 8), where, source)
    assert mechanism.recurrence_refusal(
        FileLine(str(source / 'rules.py'), 9), where, source)


def test_changed_requirement_at_the_same_manuscript_is_not_a_repeated_defect(
    tmp_path, source_repository, private_comms,
):
    manuscript = source_repository.checkout / "paper" / "manuscript.md"
    manuscript.parent.mkdir()
    manuscript.write_text("# Study\n\n## Introduction\n\nThe current introduction.\n")
    source_repository.commit_change("Publish the original manuscript", "paper/manuscript.md")
    source_repository.git(source_repository.checkout, "push", "origin", "main")
    where = FileLine("paper/manuscript.md", 3)
    earlier = replace(order(source_repository), owner=where, at=RecordedEventTime(10))
    store = private_comms.goals.todos
    colleague = private_comms.registry.declare(thread("colleague", tmp_path, 1.0))
    request = store.add_request(colleague.incarnation, "Apply the paper's 45 requested edits").id
    prior = store.create(
        "P255", source_repository.github, "Apply the paper's 45 requested edits",
        creator=colleague, assigner=colleague.incarnation, owner=colleague,
        generation=GoalId.mint(), request=request, review_at=RecordedEventTime(1000),
        order=earlier,
    )
    store.apply(prior.id, DeliverTodo(
        prior.revision, colleague.incarnation,
        OperationDelivery(at=RecordedEventTime(15)), prior.order.id,
    ))
    finished = store.get(prior.id)
    planned = replace(earlier, id=OrderId.mint(), at=RecordedEventTime(20))
    with pytest.raises(TodoError, match="the same declaration broke again"):
        store.require_recurrence_declaration(planned, where, source_repository.checkout, (), 30)

    change = PlainText(
        "P255 applied 45 edits; Tristan now requests Alyson's exact V2 introduction "
        "and selected old-introduction points in Discussion."
    )
    changed = replace(planned, scope=RequirementChangeScope(planned.scope.shape, change))
    document = FieldCodec.decode(type(changed), FieldCodec.encode(changed))
    store.require_recurrence_declaration(document, where, source_repository.checkout, (), 30)
    assert document.owner == where
    assert document.scope.source_locations() == planned.scope.source_locations()
    assert document.scope.delivery_class is CheckedDelivery
    assert change.text in "\n".join(document.scope.instructions("P266"))
    assert store.get(prior.id) == finished
    # Declaring this changed requirement never disables checks on another ordinary fix.
    with pytest.raises(TodoError, match="the same declaration broke again"):
        store.require_recurrence_declaration(planned, where, source_repository.checkout, (), 30)


def test_changed_requirement_scope_needs_its_explanation(source_repository):
    scope = RequirementChangeScope(order(source_repository).scope.shape, PlainText("New owner requirement"))
    encoded = FieldCodec.encode(scope)
    assert FieldCodec.decode(OrderScope, encoded) == scope
    del encoded["change"]
    with pytest.raises(MalformedRecord, match="change"):
        FieldCodec.decode(OrderScope, encoded)
    encoded["change"] = ""
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(OrderScope, encoded)


def test_recurrence_matches_the_enclosing_declaration_not_the_file(source):
    second = FileLine('rules.py', 9).declaration(source)
    assert second.at == FileLine(str(source / 'rules.py'), 8)
    assert second == FileLine(str(source / 'rules.py'), 8).declaration(source)
    assert second.lines == range(8, 10)
    assert second.holds(FileLine('rules.py', 8), source)
    assert not second.holds(FileLine('rules.py', 5), source)
    assert not second.holds(FileLine('other.py', 9), source)
    module = FileLine('rules.py', 1).declaration(source)
    assert module.holds(FileLine('rules.py', 5), source)
