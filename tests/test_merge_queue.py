"""Authorization/schema checks; acceptance still requires an actual queued merge."""

import unittest
from pathlib import Path
from unittest.mock import patch

from agent_comms.declared_family import UnknownFamilyMember
from agent_comms.field_codec import FieldCodec, MalformedRecord
from merge_queue import (
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
        host = Host(Path("."), "owner/repo", Path("."), False)
        with patch("merge_queue.run") as command:
            with self.assertRaises(GateError):
                host.fetch("--upload-pack=anything")
        command.assert_not_called()

    def test_unallocated_private_copies_fail_before_installer_execution(self):
        host = Host(Path("."), "owner/repo", Path("."), False)
        with patch("merge_queue.affected", return_value=["src/agent_comms/comms.py"]):
            with patch.object(Host, "command") as command:
                with self.assertRaisesRegex(GateError, "allocation is not authorized"):
                    InstallQueueCheck(123).check(host, Target("a" * 40, "b" * 40))
        command.assert_not_called()
