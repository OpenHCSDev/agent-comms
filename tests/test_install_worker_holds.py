import io
import os
import uuid
from dataclasses import dataclass

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.commands import InstallCommands, InterpreterWorkerDied
from agent_comms.installer.probes.packages import DistributionVersionsProbe, InstalledPackagesProbe
from agent_comms.installer.interpreter_operation import ProbeRequest, RuntimeProbe, serve
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


def test_worker_answers_each_request_as_that_request_s_holder(monkeypatch, tmp_path):
    holders = ("first-hold", "second-hold", "")
    lines = "".join(FieldCodec.compact(FieldCodec.encode(ProbeRequest(HolderSeenProbe(), holder)))
                    + "\n" for holder in holders)
    # serve() takes over the descriptor of its stdout, so the answers need a real file.
    answers = tmp_path / "answers"
    monkeypatch.setattr("sys.stdin", io.StringIO(lines))
    monkeypatch.setattr("sys.stdout", answers.open("w"))
    monkeypatch.setenv(HOLDER_NONCE, "the test's own hold")
    serve()
    assert [HolderSeenProbe().decode_answer(line)
            for line in answers.read_text().splitlines()] == list(holders)


def test_a_raising_probe_ends_its_worker_and_refuses_with_its_traceback():
    """The test runtime is editable, so the packages probe raises in the worker."""
    runtime = LaunchRuntime.running()
    environment = runtime.environment(os.environ)
    with InstallCommands() as commands:
        with pytest.raises(InterpreterWorkerDied) as died:
            commands.probe(InstalledPackagesProbe("agent-comms"), runtime.interpreter,
                           env=environment)
        assert not died.value.outcome.successful
        assert "ValueError: Core is editable, not a built runtime package" in died.value.errors
        assert not commands.workers
        commands.probe(DistributionVersionsProbe(), runtime.interpreter, env=environment)
        assert len(commands.workers) == 1
