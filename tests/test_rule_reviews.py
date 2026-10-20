"""Exact quotations are checked against the actual persisted draw."""

import pytest

from agent_comms.errors import ToolRefusedError
from agent_comms.rule_reviews import (
    AnsweredRuleReviewEntry,
    DrawnRuleReviewEntry,
    RuleAnswer,
    RuleReviews,
)


@pytest.mark.parametrize("answer,accepted", [
    ("“GIVE EACH FACT ONE OWNER AND DERIVE ALL” file.py:1", True),
    ("'Give each fact one owner and derive all' P275", True),
    ("Give each fact one owner and derive P275", False),
    ("Give each fact one owner and derive all", False),
    ("Give every fact one owner and derive all P275", False),
    ("Give each fact one owner and derive alligator P275", False),
    ("I applied this rule to the relevant code and verified it at file.py:1", False),
])
def test_answer_quotes_the_stored_rule_for_every_draw(tmp_path, answer, accepted):
    store = RuleReviews(tmp_path)
    rule = "Give each fact one owner and derive all consumers from it."
    store.write("lead", DrawnRuleReviewEntry((rule,)))
    entry = AnsweredRuleReviewEntry(1, answer)
    if accepted:
        store.write("lead", entry)
    else:
        with pytest.raises(ToolRefusedError, match="Quote the drawn rule's own words"):
            store.write("lead", entry)
    with store.snapshot() as db:
        saved = RuleAnswer.select(db, reviewer="lead")
    assert [row.text for row in saved] == ([answer] if accepted else [])
