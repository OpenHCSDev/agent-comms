import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.channel_targets import ChannelSelection, CommandTarget
from agent_comms.cli import main


@pytest.fixture
def run_cli(comms, monkeypatch):
    root = comms.root

    def run(*argv: str) -> tuple[int, dict]:
        code = main(["--root", str(root), *argv])
        captured = json.loads(Path("/dev/stdout").read_text()) if False else None
        return code, captured

    return run


@pytest.fixture
def cli(capsys):

    root = None

    def run(root_path, *argv: str) -> tuple[int, dict | None]:
        nonlocal root
        root = root_path
        code = main(["--root", str(root_path), *argv])
        captured = capsys.readouterr().out
        return code, (json.loads(captured) if captured.strip() else None)

    return run


class TestCliSuccess:
    def test_thread_and_poll_requests_choose_their_member_from_the_command(
            self, cli, tmp_path, monkeypatch):
        """thread counts unread unless --no-pending; poll reads the calling thread unless
        --thread names one."""
        monkeypatch.setenv("AGENT_COMMS_THREAD", "a")
        monkeypatch.setenv("PI_WORKTREE", str(tmp_path))
        for name in ("a", "b"):
            assert cli(tmp_path, "register", "--name", name, "--worktree", "/wt")[0] == 0
        assert cli(tmp_path, "send", "--from", "b", "--to", "a", "--body", "hello")[0] == 0
        _, counted = cli(tmp_path, "thread", "--name", "a")
        _, uncounted = cli(tmp_path, "thread", "--name", "a", "--no-pending")
        assert counted["pending"] == 1 and "pending" not in uncounted
        assert counted.keys() - {"pending"} == uncounted.keys()
        _, mine = cli(tmp_path, "poll")
        _, named = cli(tmp_path, "poll", "--thread", "b")
        assert (mine["thread"]["name"], mine["thread"]["pending"], len(mine["inbox"])) \
            == ("a", 1, 1)
        assert (named["thread"]["name"], named["thread"]["pending"], named["inbox"]) == ("b", 0, [])

    def test_tool_catalog_and_generic_invocation(self, cli, tmp_path, monkeypatch):
        from agent_comms.comms import Comms
        from agent_comms.pi_vocabulary import ServiceTier

        monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
        code, out = cli(tmp_path, "tools")
        assert code == 0
        assert "service_tier" not in out
        assert "comms_archive" in {tool["name"] for tool in out["tools"]}
        cli(tmp_path, "register", "--name", "a", "--worktree", "/wt", "--execution", "native")
        comms = Comms(tmp_path)
        for tier in ServiceTier:
            comms.threads.set_thread_configuration(comms.registry.require("a"), service_tier=tier)
            code, catalog = cli(tmp_path, "tools", "--name", "a")
            assert code == 0
            code, detail = cli(tmp_path, "thread", "--name", "a", "--no-pending")
            assert code == 0 and catalog["service_tier"] == detail["service_tier"] == tier.value
        code, out = cli(
            tmp_path,
            "tool",
            "--tool",
            "comms_threads",
            "--arguments",
            '{"active_only": false}',
        )
        assert code == 0 and out["threads"][0]["name"] == "a"

    def test_register_and_threads(self, cli, tmp_path):
        code, out = cli(tmp_path, "register", "--name", "a", "--worktree", "/wt", "--tags", "x,y")
        assert code == 0 and out == {"registered": "a"}
        code, out = cli(tmp_path, "threads")
        assert code == 0 and out["threads"][0]["name"] == "a"
        assert out["threads"][0]["tags"] == ["x", "y"]
        assert isinstance(out["threads"][0]["activity"], str)
        assert "activity_state" not in out["threads"][0]
        assert "activity_detail" in out["threads"][0]
        code, presence = cli(tmp_path, "who")
        assert code == 0 and presence["who"][0]["name"] == "a"
        assert presence["who"][0]["tags"] == ["x", "y"]
        assert isinstance(presence["who"][0]["activity"], str)
        assert "activity_state" not in presence["who"][0]

    def test_send_and_inbox_and_ack(self, cli, tmp_path):
        cli(tmp_path, "register", "--name", "a", "--worktree", "/wt")
        cli(tmp_path, "register", "--name", "b", "--worktree", "/wt")
        code, out = cli(
            tmp_path, "send", "--from", "a", "--to", "b", "--body", "hi", "--type", "alert"
        )
        assert code == 0 and "id" in out
        code, out = cli(tmp_path, "inbox", "--thread", "b")
        assert out["messages"][0]["text"] == "hi"
        assert out["messages"][0]["type"] == "alert"
        code, out = cli(tmp_path, "ack", "--thread", "b")
        assert out == {"acknowledged": 1}
        code, out = cli(tmp_path, "inbox", "--thread", "b")
        assert out == {"messages": []}

    def test_send_notice_and_conversation_record_channel_wake_modes(self, cli, comms):
        from agent_comms.cli_bridges.shared import Send
        from agent_comms.messages import MessageRoleName, NoticeRole
        import argparse

        comms.channels.create_tag("comms")
        for name in ("sender", "one", "two"):
            tags = frozenset() if name == "sender" else frozenset({"comms"})
            comms.registry.declare(comms.registry.new_thread(name, tags, str(comms.root)))
        comms.messaging.initialize_private_initial_protocol()
        for role, expected in (("notice", "passive"), ("conversation", "bounded_triage")):
            code, sent = cli(comms.root, "send", "--from", "sender", "--to", "#comms",
                             "--body", "public record", "--role", role)
            assert code == 0
            row = next(json.loads(line) for line in comms.bus.log.path.read_text().splitlines()
                       if json.loads(line)["id"] == sent["id"])
            manifest = row["_agent_comms_private_v1"]["initial"]
            assert len(manifest["audience"]["recipients"]) == 2
            assert [decision["wake_mode"] for decision in manifest["decisions"]] == [expected] * 2
        parser = argparse.ArgumentParser()
        Send.add_arguments(parser)
        bridged = Send.from_arguments(parser.parse_args(
            ["--to", "#comms", "--body", "record", "--role", "notice"]))
        assert isinstance(bridged.role, NoticeRole)
        assert MessageRoleName.names() == ("conversation", "notice")

    def test_acknowledge_marks_only_its_references_beside_a_reader(self, cli, tmp_path):
        import fcntl
        import threading
        from agent_comms.comms import wire

        cli(tmp_path, "register", "--name", "a", "--worktree", "/wt")
        cli(tmp_path, "register", "--name", "b", "--worktree", "/wt")
        for body in ("one", "two", "three"):
            cli(tmp_path, "send", "--from", "a", "--to", "b", "--body", body)
        comms = wire(tmp_path)
        second = comms.bus.inbox("b")[1]
        # A page reader holds the shared wire lock; an acknowledgment must not
        # wait for it, so it never queues readers behind an exclusive request.
        with open(tmp_path / ".wire.lock", "rb") as reader:
            fcntl.flock(reader, fcntl.LOCK_SH)
            acknowledged = []
            worker = threading.Thread(target=lambda: acknowledged.append(
                comms.messaging.acknowledge("b", (second.reference,))))
            worker.start()
            worker.join(10)
            assert not worker.is_alive()
        assert acknowledged == [1]
        assert [message.body for message in comms.bus.inbox("b")] == ["one", "three"]

    def test_thread_detail_and_stop_and_heartbeat(self, cli, tmp_path):
        # A CLI registration is an inbox; only an attached process connects it.
        cli(tmp_path, "register", "--name", "a", "--worktree", "/wt")
        code, out = cli(tmp_path, "thread", "--name", "a")
        assert out["name"] == "a" and out["status"] == "no_bridge"
        code, out = cli(tmp_path, "heartbeat", "--name", "a")
        assert out == {"heartbeat": "a"}
        assert cli(tmp_path, "thread", "--name", "a")[1]["status"] == "no_bridge"
        from agent_comms.child_process import ProcessIdentity
        from agent_comms.comms import wire

        with wire(tmp_path).registry.external_process("a", ProcessIdentity.capture(os.getpid())):
            assert cli(tmp_path, "thread", "--name", "a")[1]["status"] == "connected"
        assert cli(tmp_path, "thread", "--name", "a")[1]["status"] == "no_bridge"
        code, out = cli(tmp_path, "stop", "--name", "a")
        assert out == {"stopped": "a"}
        code, out = cli(tmp_path, "thread", "--name", "a")
        assert out["status"] == "no_bridge"

    def test_archive_retains_thread_declaration(self, cli, tmp_path):
        cli(tmp_path, "register", "--name", "archived", "--worktree", "/wt")
        cli(tmp_path, "stop", "--name", "archived")
        code, out = cli(tmp_path, "archive", "--name", "archived")
        assert code == 0 and out == {"archived": "archived"}

    def test_rename_self_uses_declared_caller(self, cli, tmp_path):
        cli(tmp_path, "register", "--name", "a", "--worktree", "/wt")
        code, out = cli(tmp_path, "tool", "--tool", "comms_rename_self", "--from", "a",
                        "--arguments", '{"new_name":"renamed"}')
        assert code == 0
        assert out == {"previous": "a", "current": "renamed", "changed": True}
        _, detail = cli(tmp_path, "thread", "--name", "a")
        assert detail["name"] == "renamed"

    def test_ledger_read_and_merge(self, cli, tmp_path):
        merge_file = tmp_path / "merge.json"
        merge_file.write_text(json.dumps({"k": "v"}))
        cli(tmp_path, "register", "--name", "a", "--worktree", "/wt")
        code, out = cli(tmp_path, "ledger", "--merge-from", str(merge_file), "--author", "a")
        assert out == {"merged": True}
        code, out = cli(tmp_path, "ledger")
        assert out["k"] == "v"

    def test_poll(self, cli, tmp_path):
        cli(tmp_path, "register", "--name", "a", "--worktree", "/wt")
        cli(tmp_path, "register", "--name", "b", "--worktree", "/wt")
        cli(tmp_path, "send", "--from", "a", "--to", "b", "--body", "hi")
        code, out = cli(tmp_path, "poll", "--thread", "b")
        assert out["thread"]["name"] == "b" and len(out["inbox"]) == 1

    def test_fork_missing_session_reports_error(self, cli, tmp_path, fake_native_catalog):
        code, _ = cli(
            tmp_path, "register", "--name", "a", "--worktree", "/wt", "--execution", "native"
        )
        assert code == 0
        code, out = cli(tmp_path, "fork", "--name", "kid", "--parent", "a", "--task", "t",
                        "--model", "test/model")
        assert code == 1 and out == {"type": "failed", "error": "Canonical saved session required"}


class TestCliFailClosed:
    def test_fork_cli_parent_reports_transport_error(self, cli, tmp_path, fake_native_catalog):
        code, _ = cli(tmp_path, "register", "--name", "a", "--worktree", "/wt")
        assert code == 0
        code, out = cli(tmp_path, "fork", "--name", "kid", "--parent", "a", "--task", "t",
                        "--model", "test/model")
        assert code == 1 and "no native prompt transport" in out["error"]

    def test_generic_invocation_rejects_non_object_arguments(self, cli, tmp_path):
        code, out = cli(tmp_path, "tool", "--tool", "comms_threads", "--arguments", "[]")
        assert code == 1 and "JSON object" in out["error"]

    def test_unknown_sender_is_error_exit_code(self, cli, tmp_path):
        cli(tmp_path, "register", "--name", "b", "--worktree", "/wt")
        code, out = cli(tmp_path, "send", "--from", "ghost", "--to", "b", "--body", "hi")
        assert code == 1 and "Sender" in out["error"]

    def test_unknown_thread_detail(self, cli, tmp_path):
        code, out = cli(tmp_path, "thread", "--name", "ghost")
        assert code == 1 and "not registered" in out["error"]


    def test_ledger_merge_requires_author(self, cli, tmp_path):
        merge_file = tmp_path / "merge.json"
        merge_file.write_text("{}")
        code, out = cli(tmp_path, "ledger", "--merge-from", str(merge_file))
        assert code == 1 and "author" in out["error"]

    def test_every_error_is_single_json_object(self, cli, tmp_path):
        code, out = cli(tmp_path, "thread", "--name", "ghost")
        assert isinstance(out, dict) and set(out) == {"error", "type"}
        assert out["type"] == "failed"


class TestCliSubprocess:
    """The real adapter contract: exit code, stdout JSON, parseable by shim."""

    def test_console_script_roundtrip(self, tmp_path):
        env = dict(
            __import__("os").environ,
            PYTHONPATH=str(Path(__file__).parents[1] / "src"),
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_comms.cli",
                "--root",
                str(tmp_path),
                "register",
                "--name",
                "a",
                "--worktree",
                "/wt",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
        assert json.loads(result.stdout) == {"registered": "a"}


class TestStatusCommand:
    def test_status_empty_wire(self, cli, tmp_path):
        code, out = cli(tmp_path, "status")
        assert out == {"status": []}

    def test_status_shows_activity(self, cli, tmp_path):
        from agent_comms.activity import ActivityState

        cli(tmp_path, "register", "--name", "a", "--worktree", "/wt", "--pid", str(os.getpid()))
        from agent_comms.comms import wire

        comms = wire(tmp_path)
        comms.agents.set_activity("a", ActivityState.WORKING, "bash: echo hi")
        code, out = cli(tmp_path, "status")
        row = out["status"][0]
        assert row["thread"] == "a" and row["state"] == "working"
        assert row["detail"] == "bash: echo hi"


class TestDeclaredTargetActions:
    def test_catalog_edit_rename_review_delete_and_reopen(self, cli, tmp_path):
        cli(tmp_path, 'register', '--name', 'tagged', '--worktree', str(tmp_path), '--tags', 'first')
        code, catalog = cli(tmp_path, 'target-actions', '--target', 'tagged', '--project', str(tmp_path))
        assert code == 0
        edit = next(action for action in catalog['actions'] if action['command'] == 'thread-tags')
        assert edit['parameters']['properties']['tags']['editor_default'] == 'first'
        assert 'name' not in edit['parameters']['properties']
        code, edited = cli(tmp_path, 'target-edit', '--target', 'tagged', '--operation', 'thread-tags',
                           '--arguments', '{"tags":"first,second"}')
        assert code == 0 and edited['tags'] == ['first', 'second']
        code, renamed = cli(tmp_path, 'target-edit', '--target', '#second', '--operation', 'rename-tag',
                            '--arguments', '{"new_name":"renamed"}')
        assert code == 0 and renamed['name'] == 'renamed'
        code, refused = cli(tmp_path, 'target-action', '--target', '#renamed', '--operation', 'delete-tag')
        assert code == 1 and 'Remove #renamed' in refused['error']
        code, deleted = cli(tmp_path, 'target-action', '--target', '#renamed', '--operation', 'delete-tag', '--confirmed')
        assert code == 0 and deleted['tag'] == 'renamed' and deleted['removed_tag']
        assert deleted['removed_threads'] == []
        _, detail = cli(tmp_path, 'thread', '--name', 'tagged')
        assert detail['tags'] == ['first']
        _, builtins = cli(tmp_path, 'target-actions', '--target', '#all')
        assert not {'rename-tag','delete-tag','delete-view'} & {action['command'] for action in builtins['actions']}
        code, refused = cli(tmp_path, 'target-action', '--target', 'tagged', '--operation', 'thread-tags',
                            '--arguments', '{"name":"other","tags":[]}')
        assert code == 1 and 'overridden' in refused['error']


class TestSelectedTargetActions:
    @staticmethod
    def declared(root):
        from agent_comms.comms import wire
        from agent_comms.thread_status import StoppedThreadStatus
        from agent_comms.threads import Thread

        comms = wire(root)
        for name, tags in (("alpha", {"team"}), ("beta", {"other"})):
            comms.registry.declare(comms.registry.new_thread(name, frozenset(tags), str(root)), StoppedThreadStatus())
        return comms

    def test_catalog_absent_view_actor_has_no_backend_actions(self, tmp_path):
        from agent_comms.cli_commands import ArchiveCliCommand, CliCommand, TargetEdit
        from agent_comms.errors import UnregisteredThreadError
        from agent_comms.thread_status import StoppedThreadStatus
        from agent_comms.threads import Thread

        comms = self.declared(tmp_path)
        assert CliCommand.target_catalog(comms, CliCommand.selected_targets('project'), project=str(tmp_path)) == ()
        assert CliCommand.target_catalog(comms, CliCommand.selected_targets('missing'), project=str(tmp_path)) == ()
        with pytest.raises(UnregisteredThreadError):
            TargetEdit(ArchiveCliCommand, ArchiveCliCommand.selected_targets('project'), {}).apply(comms)
        comms.registry.declare(comms.registry.new_thread('project', frozenset(), str(tmp_path)), StoppedThreadStatus())
        actions = CliCommand.target_catalog(comms, CliCommand.selected_targets('project'), project=str(tmp_path))
        assert any(action.declaration is ArchiveCliCommand for action in actions)

    def test_restart_catalog_uses_original_owner_and_idle_requirements(self, tmp_path):
        import os
        from dataclasses import replace
        from agent_comms.child_process import ProcessIdentity
        from agent_comms.cli_commands import CliCommand, RestartCliCommand, StopCliCommand
        from agent_comms.thread_execution import ExternalThreadExecution
        from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
        from agent_comms.thread_identity import TurnIdentity
        from agent_comms.turn_lease import ActiveTurn, StoredTurnRecord

        comms = self.declared(tmp_path)
        # Observe an authentic other process; this check never signals or restarts it.
        owner = replace(comms.registry.require('alpha'),
                        process_identity=ProcessIdentity.capture(os.getppid()))
        status = RunningThreadStatus()
        comms.registry.register(owner, status)
        action = next(action for action in CliCommand.target_catalog(
            comms, CliCommand.selected_targets('alpha'), project=str(tmp_path))
            if action.declaration is RestartCliCommand)
        assert action.bound == (RestartCliCommand(name='alpha'),)
        assert action.editable_fields == ()
        assert action.encode()['parameters']['properties'] == {}
        assert action.edited({}).bound == action.bound
        for arguments in ({'agent_bin': json.dumps('/other/native')}, {'agent_args': '--other'}):
            with pytest.raises(ValueError, match='cannot be overridden'):
                action.edited(arguments)
            with pytest.raises(ValueError, match='cannot be overridden'):
                RestartCliCommand.execute_target(comms, RestartCliCommand.selected_targets('alpha'), arguments)
        explicit = RestartCliCommand(name='alpha', agent_bin='/operator/native',
                                      agent_args=['--no-extensions'])
        assert explicit.agent_bin == '/operator/native'
        assert explicit.agent_args == ['--no-extensions']
        from agent_comms.cli import build_parser
        options = build_parser().parse_args([
            'restart', '--name', 'alpha', '--agent-bin', '/operator/native',
            '--agent-args=--no-extensions'])
        parsed = CliCommand.member_named(options.command).from_namespace(options)
        assert parsed == explicit
        with pytest.raises(ValueError, match='cannot be overridden'):
            action.bound[0].edited({'all': True})
        assert StopCliCommand.help == 'Stop process'
        assert not RestartCliCommand.thread_bindings(comms, owner, StoppedThreadStatus())
        busy = TurnIdentity(owner.incarnation, owner.turn_generation + 1)
        record = StoredTurnRecord.begin(
            comms.registry.store.path, busy,
            ActiveTurn('busy', owner.require_process().pid, turn_generation=busy.generation),
        )
        assert not RestartCliCommand.thread_bindings(comms,
            replace(owner, turn_record=record, turn_generation=busy.generation), status)
        assert not RestartCliCommand.thread_bindings(comms,
            replace(owner, process_identity=ProcessIdentity.capture(os.getpid())), status)
        assert not RestartCliCommand.thread_bindings(comms,
            replace(owner, execution=ExternalThreadExecution), status)

    def test_tag_batch_requires_each_confirmation_before_removing_any_tag(self, tmp_path):
        from agent_comms.cli_commands import CliCommand, DeleteTagCliCommand, TargetEdit

        comms = self.declared(tmp_path)
        selected = ('#team', '#other')
        action = next(action for action in CliCommand.target_catalog(
            comms, CliCommand.selected_targets(selected), project=str(tmp_path))
            if action.declaration is DeleteTagCliCommand)
        assert tuple(command.name for command in action.bound) == ('team', 'other')
        assert 'Remove #team' in action.confirmation
        assert '#other' in action.confirmation
        assert action.confirmation.count('Threads, saved views and history remain.') == 1
        before = comms.registry.snapshot()
        with pytest.raises(ValueError, match='Remove #team'):
            TargetEdit(DeleteTagCliCommand, DeleteTagCliCommand.selected_targets(selected), {}).apply(comms)
        assert comms.registry.snapshot() == before
        result = TargetEdit(DeleteTagCliCommand, DeleteTagCliCommand.selected_targets(selected), {}, confirmed=True).apply(comms)
        assert result.successful
        assert tuple(outcome.result.tag for outcome in result.outcomes) == ('team', 'other')
        assert all(outcome.result.removed_tag for outcome in result.outcomes)
        assert not comms.registry.require('alpha').tags
        assert not comms.registry.require('beta').tags

    def test_tag_batch_preserves_active_refusal_and_mixed_target_outcomes(self, tmp_path):
        from agent_comms.channel_management import ArchiveThreadsTagDisposition, TagDisposition
        from agent_comms.cli_commands import CliCommand, DeleteTagCliCommand, TargetEdit
        from agent_comms.field_codec import compile_decoder
        from agent_comms.thread_status import RunningThreadStatus

        comms = self.declared(tmp_path)
        comms.registry.register(comms.registry.require('beta'), RunningThreadStatus())
        selected = ('#team', '#other', 'alpha', '#all')
        action = next(action for action in CliCommand.target_catalog(
            comms, CliCommand.selected_targets(selected), project=str(tmp_path))
            if action.declaration is DeleteTagCliCommand)
        assert tuple(command.name for command in action.bound) == ('team', 'other')
        result = TargetEdit(DeleteTagCliCommand, DeleteTagCliCommand.selected_targets(selected),
            {'disposition': compile_decoder(TagDisposition).encode_editor(ArchiveThreadsTagDisposition())},
            confirmed=True).apply(comms)
        assert not result.successful
        assert tuple(outcome.target for outcome in result.outcomes) == selected
        assert result.outcomes[0].successful
        assert not any(outcome.successful for outcome in result.outcomes[1:])
        assert not comms.registry.status('alpha').visible
        assert comms.registry.status('beta').active
        assert comms.registry.require('alpha').tags == frozenset({'team'})
        assert comms.registry.require('beta').tags == frozenset({'other'})

    def test_catalog_projects_mixed_operations_and_original_editor_fields(self, tmp_path):
        from agent_comms.cli_commands import CliCommand

        comms = self.declared(tmp_path)
        actions = CliCommand.target_catalog(
            comms, CliCommand.selected_targets(("alpha", "beta", "#team")),
            ChannelSelection.decode({"alpha": ("#team",), "beta": ("#other",)}), project=str(tmp_path),
        )
        catalog = {action.declaration.declared_name: action for action in actions}
        assert set(catalog) == {"start", "stop", "archive", "pin-thread", "read-target", "delete-tag"}
        assert len(catalog["pin-thread"].bound) == 3
        assert not catalog["pin-thread"].editable_fields
        assert catalog["archive"].confirmation.startswith("Archive #team?")
        with pytest.raises(ValueError, match="Archive #team"):
            catalog["archive"].edited({}).with_confirmation(False)
        reviewed = catalog["archive"].edited({}).with_confirmation(True)
        assert reviewed.targets == ("alpha", "beta", "#team")
        assert not catalog["read-target"].encode()["parameters"]["properties"]
        assert all(command.worktree == str(tmp_path)
                   for command in catalog["read-target"].bound)

    def test_mixed_archive_reports_partial_results_in_selection_order(self, cli, tmp_path):
        from agent_comms.thread_status import RunningThreadStatus
        from agent_comms.threads import Thread

        comms = self.declared(tmp_path)
        comms.registry.declare(comms.registry.new_thread('active', frozenset({'team'}), str(tmp_path)),
                               RunningThreadStatus())
        code, result = cli(tmp_path, "target-action", "--targets", "alpha", "active", "#team",
                           "--operation", "archive", "--confirmed")
        assert code == 1
        assert [outcome["target"] for outcome in result["outcomes"]] == ["alpha", "active", "#team"]
        assert result["outcomes"][0]["result"] == {"archived": "alpha"}
        assert result["outcomes"][1]["error_type"] == "ValueError"
        assert "no longer available" in result["outcomes"][1]["error"]
        assert result["outcomes"][2]["result"]["archived"] is True
        assert not comms.registry.status("alpha").visible
        assert comms.registry.status("active").active
        assert comms.bus.require_channel("#team").archived

    def test_confirmation_and_target_override_refuse_before_any_batch_write(self, cli, tmp_path):
        comms = self.declared(tmp_path)
        original = comms.registry.snapshot()
        catalog = comms.channels.catalog.read()
        code, failure = cli(tmp_path, "target-edit", "--targets", "alpha", "#team",
                            "--operation", "archive", "--arguments", "{}")
        assert code == 1 and "Archive #team" in failure["error"]
        assert comms.registry.snapshot() == original
        assert comms.channels.catalog.read() == catalog
        code, failure = cli(tmp_path, "target-action", "--targets", "alpha", "#team",
                            "--operation", "archive", "--arguments", '{"name":"beta"}', "--confirmed")
        assert code == 1 and "overridden" in failure["error"]
        assert comms.registry.snapshot() == original
        assert comms.channels.catalog.read() == catalog

    def test_channel_stop_deduplicates_selected_member(self, tmp_path):
        from agent_comms.cli_commands import StopCliCommand, TargetBatchResult, TargetEdit
        from agent_comms.thread_status import RunningThreadStatus

        comms = self.declared(tmp_path)
        comms.channels.update_tags("beta", add=frozenset({"team"}))
        for name in ("alpha", "beta"):
            comms.registry.register(comms.registry.require(name), RunningThreadStatus())
        result = TargetEdit(StopCliCommand, StopCliCommand.selected_targets(("#team", "alpha")), {}).apply(comms)
        assert isinstance(result, TargetBatchResult) and result.successful
        assert len(result.outcomes) == 2
        assert {outcome.result.stopped for outcome in result.outcomes} == {"alpha", "beta"}
        assert all(comms.registry.status(name).stopped for name in ("alpha", "beta"))
        assert not result.reconnect_targets()

    def test_channel_start_uses_native_visible_roster_without_launching(self, tmp_path):
        from agent_comms.child_process import NoProcess
        from agent_comms.cli_commands import StartCliCommand
        from agent_comms.thread_execution import ExternalThreadExecution
        from agent_comms.thread_identity import UserThreadRole
        from agent_comms.thread_status import ArchivedThreadStatus, StoppedThreadStatus
        from agent_comms.threads import Thread

        comms = self.declared(tmp_path)
        comms.registry.declare(comms.registry.new_thread('external', frozenset({'team'}), str(tmp_path), execution=ExternalThreadExecution), StoppedThreadStatus())
        comms.registry.declare(comms.registry.new_thread('archived', frozenset({'team'}), str(tmp_path)),
                               ArchivedThreadStatus())
        comms.registry.declare(comms.registry.new_thread('human', frozenset({'team'}), str(tmp_path), role=UserThreadRole),
                               StoppedThreadStatus())
        from agent_comms.channel_targets import ChannelTarget

        snapshot = comms.registry.snapshot()
        catalog = comms.channels.catalog.read()
        target = ChannelTarget("#team")
        commands = StartCliCommand.bindings(
            comms, target, snapshot=snapshot, catalog=catalog,
            resolved=target.resolve(snapshot, comms.bus.require_channel))
        assert tuple(command.name for command in commands) == ("alpha",)
        assert all(comms.registry.require(name).process_identity == NoProcess()
                   for name in ("alpha", "external", "archived", "human"))

    def test_mixed_pins_keep_each_rows_original_channel(self, tmp_path):
        from agent_comms.cli_commands import PinThreadCliCommand, TargetEdit

        comms = self.declared(tmp_path)
        before = {name: comms.registry.require(name).tags for name in ("alpha", "beta")}
        result = TargetEdit(PinThreadCliCommand, PinThreadCliCommand.selected_targets(("alpha", "beta", "#team")), {},
                            channel=ChannelSelection.decode({"alpha": ("#team",), "beta": ("#other",)})).apply(comms)
        assert result.successful and len(result.outcomes) == 3
        catalog = comms.channels.catalog.read()
        assert comms.bus.require_channel("#team").pinned
        assert catalog.pinned_threads("#team") == {"alpha"}
        assert catalog.pinned_threads("#other") == {"beta"}
        assert {name: comms.registry.require(name).tags for name in before} == before

    def test_same_owner_memberships_pin_independently_and_archive_once(self, tmp_path):
        from agent_comms.cli_commands import ArchiveCliCommand, CliCommand, PinThreadCliCommand, TargetEdit

        comms = self.declared(tmp_path)
        comms.channels.update_tags("alpha", add=frozenset({"other"}))
        channels = {"alpha": ("#team", "#other")}
        action = next(action for action in CliCommand.target_catalog(
            comms, CliCommand.selected_targets(("alpha",)), ChannelSelection.decode(channels), project=str(tmp_path))
            if action.declaration is PinThreadCliCommand)
        assert tuple(command.channel for command in action.bound) == ("#team", "#other")
        result = TargetEdit(PinThreadCliCommand, PinThreadCliCommand.selected_targets(action.targets), {}, channel=action.channels).apply(comms)
        assert result.successful and len(result.outcomes) == 2
        document = comms.channels.catalog.read()
        assert document.pinned_threads("#team") == {"alpha"}
        assert document.pinned_threads("#other") == {"alpha"}
        assert comms.registry.require("alpha").tags == frozenset({"team", "other"})
        with pytest.raises(ValueError, match='does not accept per-target channels'):
            TargetEdit(ArchiveCliCommand, ArchiveCliCommand.selected_targets(("alpha",)), {},
                       channel=ChannelSelection.decode(channels)).apply(comms)
        archive = next(action for action in CliCommand.target_catalog(
            comms, CliCommand.selected_targets(("alpha",)), ChannelSelection.decode(channels), project=str(tmp_path))
            if action.declaration is ArchiveCliCommand)
        result = TargetEdit(ArchiveCliCommand, ArchiveCliCommand.selected_targets(("alpha",)), {}, channel=archive.channels).apply(comms)
        assert result.successful and len(result.outcomes) == 1
        assert result.outcomes[0].result.archived == "alpha"

    def test_missing_membership_reports_failure_without_losing_other_pin(self, tmp_path):
        from agent_comms.cli_commands import CliCommand, PinThreadCliCommand, TargetEdit

        comms = self.declared(tmp_path)
        comms.channels.update_tags("alpha", add=frozenset({"other"}))
        channels = {"alpha": ("#team", "#other")}
        action = next(action for action in CliCommand.target_catalog(
            comms, CliCommand.selected_targets(("alpha",)), ChannelSelection.decode(channels), project=str(tmp_path))
            if action.declaration is PinThreadCliCommand)
        comms.channels.update_tags("alpha", remove=frozenset({"team"}))
        result = TargetEdit(PinThreadCliCommand, PinThreadCliCommand.selected_targets(action.targets), {}, channel=action.channels).apply(comms)
        assert not result.successful and len(result.outcomes) == 2
        assert not result.outcomes[0].successful
        assert "#team" in result.outcomes[0].error
        assert result.outcomes[1].successful
        document = comms.channels.catalog.read()
        assert not document.pinned_threads("#team")
        assert document.pinned_threads("#other") == {"alpha"}

    def test_mixed_read_marks_human_views_without_advancing_executor_delivery(self, tmp_path):
        from agent_comms.cli_commands import ReadTargetCliCommand, TargetEdit
        from agent_comms.threads import Thread
        from native_unread_fixture import completed_snapshot
        from agent_comms.ui_model.unread import ReplyUnread

        comms = self.declared(tmp_path)
        source = tmp_path / 'alpha.jsonl'
        source.write_text(json.dumps({'type': 'message', 'timestamp': '2026-09-20T00:00:01Z', 'message': {
            'role': 'assistant', 'content': [{'type': 'text', 'text': 'Saved reply'}],
        }}) + '\n')
        comms.registry.declare(comms.registry.new_thread('alpha', frozenset({'team'}), str(tmp_path), session_file=str(source)))
        comms.channels.update_tags('beta', add=frozenset({'team'}))
        viewer = comms.messaging.user_identity(str(tmp_path)).name
        comms.messaging.send('alpha', 'beta', 'Executor message').message.message_id
        comms.messaging.send('alpha', viewer, 'Human DM').message.message_id
        comms.messaging.send('alpha', '#team', 'Channel message').message.message_id
        before = completed_snapshot(comms, str(tmp_path))
        assert before.thread_unread['alpha'] == ReplyUnread(1)
        assert before.unread['alpha'] == 1
        # The canonical tag change also publishes its membership notification.
        assert before.channel_unread['#team'] == 2
        result = TargetEdit(ReadTargetCliCommand, ReadTargetCliCommand.selected_targets(('alpha', '#team')),
                            {}, project=str(tmp_path)).apply(comms)
        assert result.successful and tuple(item.result.read for item in result.outcomes) == ('alpha', '#team')
        after = completed_snapshot(comms, str(tmp_path))
        assert after.thread_unread['alpha'] == ReplyUnread(0)
        assert after.unread.get('alpha', 0) == 0
        assert after.channel_unread['#team'] == 0
        assert comms.bus.pending_count('beta', CommandTarget.named('alpha')) == 1
        # DeliveryScope excludes beta's own membership notice from its inbox.
        assert comms.bus.pending_count('beta', CommandTarget.named('#team')) == 1

    def test_catalog_and_execution_keep_missing_and_single_only_targets_truthful(self, tmp_path):
        from agent_comms.cli_commands import ArchiveCliCommand, CliCommand, ForkCliCommand, TargetEdit

        comms = self.declared(tmp_path)
        actions = CliCommand.target_catalog(comms, CliCommand.selected_targets(("missing", "alpha")), project=str(tmp_path))
        assert {action.declaration.declared_name for action in actions} >= {"archive", "read-target"}
        result = TargetEdit(ArchiveCliCommand, ArchiveCliCommand.selected_targets(("missing", "alpha")), {}).apply(comms)
        assert not result.successful
        assert result.outcomes[0].target == "missing" and not result.outcomes[0].successful
        assert result.outcomes[1].result.archived == "alpha"
        with pytest.raises(ValueError, match="single target"):
            TargetEdit(ForkCliCommand, ForkCliCommand.selected_targets(("alpha", "beta")), {}).apply(comms)


def test_result_kind_decides_exit_code_and_navigation_targets():
    from agent_comms.child_process import NoProcess
    from agent_comms.cli_commands import (
        NewThreadCliCommand, StartCliCommand, TargetBatchResult, TargetCompleted,
        TargetFailed, ThreadCreatedResult, ThreadStoppedResult,
    )
    from agent_comms.owner_lifecycle import OwnerStartResult

    launched = OwnerStartResult("alpha", 11, True)
    attached = OwnerStartResult("beta", 12, False)
    batch = TargetBatchResult((
        TargetCompleted("alpha", StartCliCommand(name="alpha"), launched),
        TargetCompleted("beta", StartCliCommand(name="beta"), attached),
        TargetFailed("gamma", "ValueError", "gone"),
    ))
    assert launched.reconnect_targets() == ("alpha",) and attached.reconnect_targets() == ()
    assert batch.reconnect_targets() == ("alpha",)
    assert batch.exit_code() == 1
    assert TargetBatchResult(batch.outcomes[:2]).exit_code() == 0
    assert ThreadStoppedResult("alpha").exit_code() == 0
    assert not launched.open_targets() and not batch.open_targets()
    created = ThreadCreatedResult("child", NoProcess())
    assert created.open_targets() == ("child",)
    command = NewThreadCliCommand(name="child", project="/project", model=None)
    completed = TargetCompleted("/project", command, created)
    assert TargetBatchResult((completed, batch.outcomes[-1], completed)).open_targets() == ("child",)
