"""Original-input cases share their reservation implementation."""

import pytest

from agent_comms.execution.turn_input_source import OriginalTurnInput

pytestmark = pytest.mark.refactor_guard


def test_original_input_cases_inherit_reservation():
    """New input cases cannot silently opt out of the shared original path."""
    cases = list(OriginalTurnInput.__subclasses__())
    while cases:
        case = cases.pop()
        cases.extend(case.__subclasses__())
        assert case.reserve is OriginalTurnInput.reserve, case
