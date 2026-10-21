"""Core owns what an order and a strike may say, for every caller."""
import pytest
from dataclasses import replace

from test_todos import order as checked_order
from agent_comms.commander.order import OrderFile
from agent_comms.todos import BluntText, FairText, HierarchyText, OrderText, PlainText, StrikeReason

TEN = "the unit pushed a commit that deleted the store test file"


@pytest.mark.parametrize(("family", "text", "accepted"), [
    (PlainText, TEN, True),
    (PlainText, "Merged the store fix", True),
    (StrikeReason, TEN, True),
    (StrikeReason, "too short to say anything", False),
    (PlainText, TEN + ' and quoted "custody" from the old order', True),
    (PlainText, TEN + " and named `lifecycle.py` in backticks", True),
    (PlainText, TEN + " in src/agent_comms/lifecycle/owner.py", True),
    (OrderText, "Delete the second store and its writes", True),
    (OrderText, "Please delete the second store", False),
    (OrderText, "Maybe consider deleting the store", False),
    (OrderText, 'Delete the line that says "if possible"', True),
    (BluntText, "The second store is slop because Core already owns the rows", True),
    (BluntText, "The second store restates the rows Core owns", False),
    (FairText, "Fold the store into its parent", True),
    (FairText, "Prefer composition over inheritance for the store", False),
    (HierarchyText, "Fold the forwarder into the owner", True),
    (HierarchyText, "Keep the store forwarding to Core", False),
    (HierarchyText, "Use a new base class", True),
])
def test_value_types_refuse_their_declared_words(family, text, accepted):
    if accepted:
        assert family(text).text == text
    else:
        with pytest.raises(ValueError):
            family(text)


def test_soft_word_rule_reads_only_the_two_explanation_fields(source_repository):
    order = replace(checked_order(source_repository),
                    edits=('Delete maybe.py',), banned=('optional guards', 'perhaps'),
                    report='Report optional examples')
    document = OrderFile(order,
        'The command refuses a check that counts deleted source declarations.',
        'This is stupid because two rules answer the same question differently.')
    assert document.problems_found(clean=False) == []
    order.require_wording()
    for field in ('defect', 'why_stupid'):
        refused = replace(document, **{field: 'Maybe ' + getattr(document, field)})
        assert any(problem.startswith(field + ':') for problem in refused.problems_found(clean=False))
