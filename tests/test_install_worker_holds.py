import io
import os
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pytest

from agent_comms.cli_commands import StartCliCommand, StopCliCommand
from agent_comms.field_codec import ClaimId, FieldCodec
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.interpreter_operation import (
    ProbeReply,
    ProbeRequest,
    RuntimeProbe,
    serve,
)
from agent_comms.installer.owner_starts import OwnerStart, RefusedOwner, StartedOwner
from agent_comms.installer.probes.commands import CliCommandProbe, CliOutput
from agent_comms.installer.probes.owners import OwnerActionsProbe
from agent_comms.installer.probes.packages import DistributionVersionsProbe, InstalledPackagesProbe
from agent_comms.installer.refusals import InterpreterWorkerDied, UpgradeRefused
from agent_comms.installer.upgrade import Stack
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.maintenance_barrier import HOLDER_NONCE, MaintenanceClaimHolder


@pytest.mark.parametrize("command", [StartCliCommand, StopCliCommand])
def test_owner_actions_decode_in_the_selected_worker(comms, command):
    runtime = LaunchRuntime.running()
    with InstallCommands() as commands:
        stack = Stack(Path(__file__).resolve().parents[1], commands, runtime=runtime)
        outcomes = stack.owners(comms.root, command, ["missing-owner"])
    assert len(outcomes) == 1
    assert outcomes[0].name == "missing-owner"
    with pytest.raises(UpgradeRefused):
        outcomes[0].require_success(UpgradeRefused)


@pytest.mark.parametrize("probe_type", [CliCommandProbe, OwnerActionsProbe])
def test_cli_reply_preserves_declared_failure_from_each_entry(comms, probe_type):
    runtime = LaunchRuntime.running()
    with InstallCommands() as commands:
        stack = Stack(Path(__file__).resolve().parents[1], commands, runtime=runtime)
        probe = probe_type(("--root", str(comms.root), "not-a-command"))
        with pytest.raises(UpgradeRefused, match="invalid choice") as refused:
            stack.cli_reply(probe, partial(CliOutput.decoded, result=tuple[OwnerStart, ...]))
    assert "undecodable answer" not in str(refused.value)


def test_owner_reports_own_their_refusal():
    StartedOwner("ready").require_success(UpgradeRefused)
    with pytest.raises(UpgradeRefused):
        RefusedOwner("missing", "not registered").require_success(UpgradeRefused)


def test_probes_under_successive_holds_share_one_worker():
    runtime = LaunchRuntime.running()
    with InstallCommands() as commands:
        for _ in range(2):
            with MaintenanceClaimHolder(ClaimId.mint()).acting():
                commands.probe(DistributionVersionsProbe(), runtime.interpreter,
                               env=runtime.environment(os.environ))
        assert len(commands.workers) == 1
        (key,) = commands.workers
        assert HOLDER_NONCE not in dict(key.environment)


def test_worker_answers_each_request_as_that_request_s_holder(monkeypatch, tmp_path):
    # The probe is a family member only while this test runs, so later tests reading the
    # installer's probes (OutgoingWire) never see it.
    @dataclass(frozen=True)
    class HolderSeenProbe(RuntimeProbe[str]):
        def run(self) -> str:
            return os.environ.get(HOLDER_NONCE, "")

    try:
        holders = (ClaimId.mint(), ClaimId.mint(), "")
        lines = "".join(
            FieldCodec.compact(FieldCodec.encode(ProbeRequest(HolderSeenProbe(), holder))) + "\n"
            for holder in holders
        )
        # serve() takes over the descriptor of its stdout, so the answers need a real file.
        answers = tmp_path / "answers"
        monkeypatch.setattr("sys.stdin", io.StringIO(lines))
        monkeypatch.setattr("sys.stdout", answers.open("w"))
        monkeypatch.setenv(HOLDER_NONCE, ClaimId.mint())
        serve()
        assert [FieldCodec.decode(HolderSeenProbe().answer_type(),
                                  FieldCodec.decode(ProbeReply, FieldCodec.parse(line)).answer)
                for line in answers.read_text().splitlines()] == list(holders)
    finally:
        RuntimeProbe.__registry__.pop(HolderSeenProbe.declared_name)


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
