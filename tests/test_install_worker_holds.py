import io
import json
import os
import uuid
from dataclasses import dataclass

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.interpreter_operation import (
    CompletedProbeReply,
    DistributionVersionsProbe,
    ProbeReply,
    ProbeRequest,
    RuntimeProbe,
    serve,
)
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.maintenance_barrier import HOLDER_NONCE, MaintenanceHolder


@dataclass(frozen=True)
class HolderSeenProbe(RuntimeProbe[str]):
    def run(self) -> str:
        return os.environ.get(HOLDER_NONCE, "")


def test_probes_under_successive_holds_share_one_worker():
    runtime = LaunchRuntime.running()
    with InstallCommands() as commands:
        for _ in range(2):
            with MaintenanceHolder(uuid.uuid4().hex).acting():
                commands.probe(DistributionVersionsProbe(), runtime.interpreter,
                               env=runtime.environment(os.environ))
        assert len(commands.workers) == 1
        (key,) = commands.workers
        assert HOLDER_NONCE not in dict(key.environment)


def test_worker_answers_each_request_as_that_request_s_holder(monkeypatch):
    holders = ("first-hold", "second-hold", "")
    lines = "".join(FieldCodec.compact(FieldCodec.encode(ProbeRequest(HolderSeenProbe(), holder)))
                    + "\n" for holder in holders)
    answers = io.StringIO()
    monkeypatch.setattr("sys.stdin", io.StringIO(lines))
    monkeypatch.setattr("sys.stdout", answers)
    monkeypatch.setenv(HOLDER_NONCE, "the test's own hold")
    serve()
    replies = [FieldCodec.decode(ProbeReply, json.loads(line))
               for line in answers.getvalue().splitlines()]
    assert all(isinstance(reply, CompletedProbeReply) for reply in replies)
    assert [HolderSeenProbe().decode_answer(reply.answer()) for reply in replies] == list(holders)
