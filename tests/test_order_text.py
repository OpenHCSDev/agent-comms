"""Core owns what an order and a strike may say, for every caller."""
from dataclasses import replace

import pytest

from agent_comms.planning.order_words import (
    BluntText,
    FairText,
    HierarchyText,
    OrderText,
    PlainText,
    StrikeReason,
)
from agent_comms.team.order import OrderFile
from test_todos import order as checked_order

TEN = "the teammate pushed a commit that deleted the store test file"


@pytest.mark.parametrize(("family", "text", "accepted"), [
    (PlainText, TEN, True),
    (PlainText, "Merged the store fix", True),
    (StrikeReason, TEN, True),
    (StrikeReason, "   ", False),
    (PlainText, TEN + ' and quoted "custody" from the old order', True),
    (PlainText, TEN + " and named `lifecycle.py` in backticks", True),
    (PlainText, TEN + " in src/agent_comms/lifecycle/owner.py", True),
    (OrderText, "Delete the second store and its writes", True),
    (OrderText, "Please delete the second store", True),
    (OrderText, "   ", False),
    (BluntText, "The second store is slop because Core already owns the rows", True),
    (BluntText, "The second store restates the rows Core owns", True),
    (BluntText, "   ", False),
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


def test_order_file_checks_substantive_rules_without_word_policing(source_repository):
    order = replace(checked_order(source_repository),
                    edits=('Delete maybe.py',), banned=('optional guards', 'perhaps'),
                    report='Report optional examples')
    document = OrderFile(order,
        'Counter store duplicates rows.',
        'Two stores disagree on one counter.')
    assert document.problems_found(clean=False) == []
    order.require_wording()
    dogma = replace(document, why_stupid='Prefer composition over inheritance here.')
    assert any(problem.startswith('why_stupid:') for problem in dogma.problems_found(clean=False))
