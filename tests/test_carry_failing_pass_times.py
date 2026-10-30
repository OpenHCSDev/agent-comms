"""The real carry gives each stored failure its newest-failure time, once."""

import json

from cutover.carry_failing_pass_times import FailingPassTimes

from agent_comms.locked_store import LiveFilesRequest
from agent_comms.service_passes import ServicePassStore


def test_carry_gives_a_stored_failure_its_first_failure_time_once(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    path = ServicePassStore.path_in(root)
    path.write_text(json.dumps({
        "failing": {"CommanderHeartbeat": {"since": 1793331540.0, "error": "TypeError: x"}},
        "commander_completed": [[{"name": "lead", "created_at": 1792300653.5}, 1793350616.1]]}))
    step = FailingPassTimes(LiveFilesRequest(root), tmp_path / "spill")

    assert step.carry(apply=False).changed == 1
    assert step.carry(apply=True).rewritten == (path.relative_to(root),)
    failure = ServicePassStore(path).read().failing["CommanderHeartbeat"]
    assert (failure.since, failure.failed_at) == (1793331540.0, 1793331540.0)
    assert not failure.failed_since(1793331600.0)
    assert step.carry(apply=True).changed == 0
