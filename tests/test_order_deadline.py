"""Measured order timing uses retained deliveries and the real commander entrypoint."""
import json
import math
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import DoneTodoState, MeasuredDeadline, OperationDelivery, TodoStore
from test_commander_problem_orders import create, order, order_body


@pytest.mark.parametrize('rates', ((), (15.0,)))
def test_measured_deadline_rounds_up_and_keeps_two_minute_floor(tmp_path, rates):
    measured = MeasuredDeadline(rates)
    assert measured.deadline(order(tmp_path, 'new-order')).at == 240


def test_empty_measurement_does_not_create_a_store(tmp_path):
    store = TodoStore(tmp_path)
    measured = MeasuredDeadline.read(store)
    assert measured.rates == ()
    assert not store.path.exists()


