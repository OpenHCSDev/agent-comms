"""Report the original holder card when the two retained baseline tests fail."""

import json

import pytest

from agent_comms.ui_model.teammate_state import TeammateState


class HolderTrace:
    def pytest_exception_interact(
        self, node: pytest.Function, call: pytest.CallInfo, report: pytest.TestReport,
    ) -> None:
        comms = node.funcargs["held_bus"]
        frame = node.module.capture_frame(comms, node.module.ProgressRead())
        for source in TeammateState.observations(comms, frame):
            if source.thread.name == "worker":
                card = TeammateState.read(source)
                print("HOLDER_TRACE " + json.dumps({
                    "node": node.nodeid,
                    "selected": type(card).__name__,
                    "presence": source.presence.declared_name,
                    "pending": [todo.id for todo in source.pending],
                    "execution": type(source.goal.execution).__name__,
                    "unit_blocks": [type(block).__name__ for block in source.goal.execution.unit_blocks()],
                    "detail": card.detail(),
                }, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(pytest.main([
        "-q", "-s", "-p", "no:xdist", "-o", "faulthandler_timeout=60",
        "/home/ts/code/projects/agent-comms/tests/test_holder_standing_rules.py::test_failed_holder_is_reported_without_reissuing_orders_or_replaying_unknown",
        "/home/ts/code/projects/agent-comms/tests/test_holder_standing_rules.py::test_one_state_per_teammate_even_when_blocked_and_detached",
    ], plugins=[HolderTrace()]))
