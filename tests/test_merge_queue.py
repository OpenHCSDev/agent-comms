"""Authorization/schema checks; acceptance still requires an actual queued merge."""

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_comms.declared_family import UnknownFamilyMember
from agent_comms.field_codec import FieldCodec, MalformedRecord
from cutover.backup import UpgradeRefused
from install_gate import FailedMachineResult, GateResult, MachinesGateResult
from merge_queue import (
    CheckFailed,
    DispatchQueueCheck,
    GateError,
    Host,
    InstallQueueCheck,
    QueueCheck,
    SrcQueueCheck,
    Target,
    WorkflowDispatchRunEvent,
    WorkflowRun,
)


class MergeQueueTests(unittest.TestCase):
    def test_declared_check_is_the_wire_decoder(self):
        check = QueueCheck.member_named("src").from_tokens(["123", "src"])
        self.assertIs(type(check), SrcQueueCheck)
        self.assertEqual(check.run_id, 123)
        with self.assertRaises(UnknownFamilyMember):
            QueueCheck.member_named("ratchet")

    def test_github_event_decodes_to_its_behavior(self):
        record = FieldCodec.decode(WorkflowRun, {
            "event": "workflow_dispatch", "head_sha": "a" * 40,
            "head_branch": "feature", "status": "in_progress",
            "path": ".github/workflows/debt-ratchet.yml", "actor": {"login": "owner"},
        })
        self.assertIs(record.event, WorkflowDispatchRunEvent)
        with self.assertRaises(MalformedRecord):
            FieldCodec.decode(WorkflowRun, {
                "event": "pull_request", "head_sha": "a" * 40,
                "head_branch": "feature", "status": "in_progress",
                "path": ".github/workflows/debt-ratchet.yml", "actor": {"login": "owner"},
            })

    def test_a_workflow_cannot_supply_git_options(self):
        host = Host(Path("."), "owner/repo", Path("."))
        with patch("merge_queue.run") as command:
            with self.assertRaises(GateError):
                host.fetch("--upload-pack=anything")
        command.assert_not_called()

    def test_foreign_status_does_not_dispatch_more_checks(self):
        host = Host(Path("."), "owner/repo", Path("."))
        with patch("merge_queue.StatusWorkflowRun.require"):
            with patch.object(Host, "fetch") as fetch:
                DispatchQueueCheck(123, "a" * 40, "unrelated").execute(host)
        fetch.assert_not_called()

    def test_gate_result_carries_failure_without_successful_private_steps(self):
        result = MachinesGateResult({"desktop": FailedMachineResult("Insufficient room")})
        decoded = FieldCodec.decode(GateResult, FieldCodec.encode(result))
        self.assertEqual(decoded.failures, ("desktop: Insufficient room",))

    def test_ratchet_failure_preserves_measure_and_exact_source_lines(self):
        completed = subprocess.CompletedProcess(
            ("ratchet",), 1,
            stdout=json.dumps({"introduced_lines": {"NoneAbsence:tools/example.py": [17, 21]},
                               "delta": {"NoneAbsence:tools/example.py": 2}}), stderr="",
        )
        error = CheckFailed(SrcQueueCheck(123), completed)
        self.assertEqual(str(error),
                         "Ratchet: NoneAbsence tools/example.py:17; NoneAbsence tools/example.py:21")

    def test_member_limit_is_passed_to_the_command(self):
        host = Host(Path("."), "owner/repo", Path("."))
        completed = subprocess.CompletedProcess(("true",), 0, stdout="", stderr="")
        with patch("merge_queue.subprocess.run", return_value=completed) as command:
            host.command(SrcQueueCheck(123), "true")
        self.assertEqual(command.call_args.kwargs["timeout"], 600)
        self.assertEqual(InstallQueueCheck.limit, 3000)

    def test_practice_install_is_not_skipped_by_a_second_storage_policy(self):
        host = Host(Path("."), "owner/repo", Path("."))
        with patch.object(Host, "command") as command:
            check = InstallQueueCheck(123)
            check.check(host, Target("a" * 40, "b" * 40))
        self.assertIs(command.call_args.args[0], check)
        self.assertIn("--revision", command.call_args.args)

    def test_only_declared_installer_refusal_crosses_the_log_boundary(self):
        error = UpgradeRefused("Insufficient room")
        self.assertEqual(UpgradeRefused.public_message("private trace\n" + error.output),
                         error.output)
        secret = "PRIVATE_PAYLOAD_DO_NOT_LEAK"
        self.assertNotIn(secret, UpgradeRefused.public_message(secret))
        private_failure = f"upgrade-bus: aborted before the backup (RuntimeError: {secret})"
        self.assertNotIn(secret, UpgradeRefused.public_message(private_failure))
