"""The commander's first review of an order is measured from retained deliveries."""

import pytest

from agent_comms.todos import MeasuredReview, TodoStore
from test_commander_problem_orders import order


@pytest.mark.parametrize('rates', ((), (15.0,)))
def test_measured_review_rounds_up_and_keeps_two_minute_floor(tmp_path, rates):
    measured = MeasuredReview(rates)
    assert measured.review_at(order(tmp_path, 'new-order')).at == 240


def test_empty_measurement_does_not_create_a_store(tmp_path):
    store = TodoStore(tmp_path)
    measured = MeasuredReview.read(store)
    assert measured.rates == ()
    assert not store.path.exists()
