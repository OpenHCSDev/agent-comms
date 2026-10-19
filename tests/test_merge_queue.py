"""Authorization/schema checks; acceptance still requires an actual queued merge."""

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_comms.declared_family import UnknownFamilyMember
from agent_comms.field_codec import FieldCodec, MalformedRecord
from install_gate import FailedMachineResult, GateResult, MachinesGateResult
from merge_queue import (
    DispatchQueueCheck,
    GateError,
    Host,
    InstallQueueCheck,
    QueueCheck,
    SrcQueueCheck,
    Target,
    WorkflowDispatchRunEvent,
    WorkflowRun,
    annotation,
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
        host = Host(Path("."), "owner/repo", Path("."), False)
        with patch("merge_queue.run") as command:
            with self.assertRaises(GateError):
                host.fetch("--upload-pack=anything")
        command.assert_not_called()

    def test_foreign_status_does_not_dispatch_more_checks(self):
        host = Host(Path("."), "owner/repo", Path("."), False)
        with patch("merge_queue.StatusWorkflowRun.require"):
            with patch.object(Host, "fetch") as fetch:
                DispatchQueueCheck(123, "a" * 40, "unrelated").execute(host)
        fetch.assert_not_called()

    def test_gate_result_carries_failure_without_successful_private_steps(self):
        result = MachinesGateResult({"desktop": FailedMachineResult("Insufficient room")})
        decoded = FieldCodec.decode(GateResult, FieldCodec.encode(result))
        self.assertEqual(decoded.failures, ("desktop: Insufficient room",))

    def test_annotation_escapes_protocol_delimiters(self):
        output = io.StringIO()
        with redirect_stdout(output):
            annotation("tools/example.py", 17, "type mismatch\nnew line")
        self.assertEqual(output.getvalue(),
                         "::error file=tools/example.py,line=17::type mismatch%0Anew line\n")

    def test_unallocated_private_copies_fail_before_installer_execution(self):
        host = Host(Path("."), "owner/repo", Path("."), False)
        with patch("merge_queue.affected", return_value=["src/agent_comms/comms.py"]):
            with patch.object(Host, "command") as command:
                with self.assertRaisesRegex(GateError, "allocation is not authorized"):
                    InstallQueueCheck(123).check(host, Target("a" * 40, "b" * 40))
        command.assert_not_called()
