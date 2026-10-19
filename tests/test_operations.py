
import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from agent_comms.activity import ActivityState
from agent_comms.agent_events import AgentInfo, ContextUsageObservation
from agent_comms.child_process import ParentedProcess, ProcessIdentity
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.launch_runtime import CallingThreadUnavailable, ThreadLaunch
from agent_comms.message_page import PageBounds
from agent_comms.messages import QuestionKind
from agent_comms.pi_counts import PiCount
from agent_comms.pi_vocabulary import HighThinkingLevel
from agent_comms.read_basis import NamedViewer
from agent_comms.thread_management import ForkSpec, NewThreadSpec
from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller


def test_calling_thread_is_declared_by_owner_launch_environment():
    assert ThreadLaunch.current({"AGENT_COMMS_THREAD": "owner"}).name == "owner"
    with pytest.raises(CallingThreadUnavailable, match="AGENT_COMMS_THREAD"):
        ThreadLaunch.current({})
    assert not ThreadLaunch.called_by("owner", lambda caller: caller, environment={})


@pytest.mark.parametrize("task", ["", "first input"])
def test_owner_launch_carries_startup_input_exactly_when_given_and_worker_takes_it_once(
    comms, tmp_path, monkeypatch, task
):
    launched = []
    launch = ParentedProcess.launch

    def record(command, *, env, **kwargs):
        launched.append(dict(env))
        return launch((sys.executable, "-c", ""), env=env, **kwargs)

    monkeypatch.setattr(ParentedProcess, "launch", record)
    # An inherited key belongs to another launch and never reaches this child.
    monkeypatch.setenv("AGENT_COMMS_STARTUP_INPUT_KEY", "inherited")
    comms.threads.create(NewThreadSpec.from_task(
        name="child", project=str(tmp_path), model="test/model",
        thinking_level=HighThinkingLevel, task=task,
    ))
    child, = launched
    key = ThreadLaunch.current(child).startup_input
    assert ("AGENT_COMMS_STARTUP_INPUT_KEY" in child) == bool(task)
    assert key != "inherited" and (key is not None) == bool(task)
    assert ThreadLaunch.take_startup_input(child) == key
    assert "AGENT_COMMS_STARTUP_INPUT_KEY" not in child
    assert ThreadLaunch.take_startup_input(child) is None


class TestMessaging:
    def test_send_routes_through_bus(self, wired):
        mid = wired.messaging.send("PR111", "fixer", "hello", QuestionKind).message.message_id
        assert isinstance(mid, str)
        assert wired.bus.pending_count("fixer") == 1

    def test_send_fail_closed_unknown_sender(self, wired):
        with pytest.raises(UnregisteredThreadError, match="Sender"):
            wired.messaging.send("ghost", "fixer", "hello").message.message_id

    def test_broadcast_reaches_all_peers(self, wired):
        wired.messaging.send("PR111", "#all", "green").message.message_id
        assert wired.bus.pending_count("fixer") == 1

    def test_ack_clears_inbox(self, wired):
        wired.messaging.send("PR111", "fixer", "hello").message.message_id
        messages = wired.bus.inbox("fixer")
        references = tuple(message.reference for message in messages)
        assert wired.messaging.acknowledge("fixer", references) == 1
        assert wired.bus.pending_count("fixer") == 0
        assert wired.messaging.acknowledge("fixer", references) == 0

    def test_scoped_ack_only_clears_selected_conversation(self, wired):
        wired.messaging.send("PR111", "fixer", "direct").message.message_id
        wired.messaging.send("PR111", "#all", "global").message.message_id

        references = tuple(message.reference for message in wired.bus.inbox("fixer", "PR111"))
        assert wired.messaging.acknowledge("fixer", references) == 1
        assert wired.bus.pending_count("fixer", "PR111") == 0
        assert wired.bus.pending_count("fixer", "#all") == 1
        assert wired.bus.pending_count("fixer") == 1

    def test_scoped_channel_ack_does_not_clear_dm(self, wired):
        wired.messaging.send("PR111", "fixer", "direct").message.message_id
        wired.messaging.send("PR111", "#all", "global").message.message_id

        references = tuple(message.reference for message in wired.bus.inbox("fixer", "#all"))
        assert wired.messaging.acknowledge("fixer", references) == 1
        assert wired.bus.pending_count("fixer", "#all") == 0
        assert wired.bus.pending_count("fixer", "PR111") == 1
        assert wired.bus.pending_count("fixer") == 1

    def test_inbox_order_follows_seq(self, wired):
        for i in range(5):
            wired.messaging.send("PR111", "fixer", f"m{i}").message.message_id
        bodies = [m.body for m in wired.bus.inbox("fixer")]
        assert bodies == [f"m{i}" for i in range(5)]

    def test_opened_pending_counts_groups_all_conversations_in_one_result(self, wired):
        wired.messaging.send("PR111", "fixer", "direct one").message.message_id
        wired.messaging.send("PR111", "fixer", "direct two").message.message_id
        wired.messaging.send("PR111", "#all", "global").message.message_id

        with wired.bus.snapshot(viewer=NamedViewer("fixer")) as (basis, source):
            assert wired.bus.pending_counts_opened(source, basis) == {"PR111": 2, "#all": 1}
        references = tuple(message.reference for message in wired.bus.inbox("fixer", "PR111"))
        wired.messaging.acknowledge("fixer", references)
        with wired.bus.snapshot(viewer=NamedViewer("fixer")) as (basis, source):
            assert wired.bus.pending_counts_opened(source, basis) == {"#all": 1}

    def test_shared_history_page_contract(self, wired):
        for index in range(5):
            wired.messaging.send("PR111", "#all", f"m{index}").message.message_id

        page = wired.views.channel_history_page("#all", PageBounds(limit=2))
        assert [message.body for message in page.messages] == ["m3", "m4"]
        assert page.has_older
        assert wired.bus.log.latest_sequence() == 5


class TestThreadOps:
    def test_thread_transcript_normalizes_persisted_pi_events(self, wired, tmp_path):
        session_file = tmp_path / "session.jsonl"
        records = [
            {
                "type": "message",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "demo request"}],
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "considering"},
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "comms_send",
                            "arguments": {"to": "child"},
                        },
                    ],
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "comms_send",
                    "content": [{"type": "text", "text": "sent"}],
                    "isError": False,
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "demo complete"}],
                },
            },
        ]
        session_file.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        wired.registry.declare(
            wired.registry.new_thread(name='transcript-thread', tags=frozenset(), worktree=str(tmp_path), session_file=str(session_file))
        )

        events = wired.transcripts.thread_transcript_page("transcript-thread").events

        assert [event.declared_name for event in events] == [
            "user",
            "thinking",
            "tool_start",
            "tool_end",
            "assistant",
        ]
        assert events[2].raw_input == {"to": "child"}
        assert events[3].text == "sent"
        row = next(row for row in wired.views.presence() if row["name"] == "transcript-thread")
        assert row["resumable"] is True

    def test_thread_transcript_includes_saved_compaction_summary(self, wired, tmp_path):
        session_file = tmp_path / "compacted.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "type": "compaction",
                    "summary": "Important decisions and remaining work.",
                    "tokensBefore": 12000,
                }
            )
            + "\n"
        )
        wired.registry.declare(
            wired.registry.new_thread(name='compacted-thread', tags=frozenset(), worktree=str(tmp_path), session_file=str(session_file))
        )

        events = wired.transcripts.thread_transcript_page("compacted-thread").events

        assert len(events) == 1
        assert events[0].declared_name == "notice"
        assert "Important decisions" in events[0].text

    def test_claimed_thread_inbox_starts_at_its_creation(self, wired):
        wired.messaging.send("PR111", "#all", "before claim").message.message_id
        claimed = wired.threads.claim_thread(
            "viewer",
            tags=frozenset({"acp"}),
            worktree="/tmp/project",
        )
        assert wired.bus.inbox(claimed.name) == []
        wired.messaging.send("PR111", "#all", "after claim").message.message_id
        assert [message.body for message in wired.bus.inbox(claimed.name)] == ["after claim"]

    def test_presence_uses_configured_model_with_runtime_context(self, wired):
        wired.threads.set_thread_configuration(
            wired.registry.require("fixer"), model="configured/model"
        )
        wired.agents.observe_runtime_info("fixer", ContextUsageObservation(context_used=PiCount.from_wire(25)))
        wired.agents.observe_runtime_info(
            "fixer", AgentInfo(model="observed/model", context_size=PiCount.from_wire(100))
        )
        row = next(row for row in wired.views.who() if row["name"] == "fixer")
        assert row["model"] == "configured/model"
        assert (row["context_used"], row["context_size"]) == (25, 100)
        assert wired.agents.agent_info_of("fixer").context_used == PiCount.from_wire(25)

    def test_configuration_names_the_resolved_model(self, wired):
        wired.agents.observe_runtime_info("fixer", ContextUsageObservation(context_used=PiCount.from_wire(25)))
        wired.agents.observe_runtime_info("fixer", AgentInfo(model="observed/model", context_size=PiCount.from_wire(100)))
        answer = wired.configuration.answer("fixer")
        assert answer.model == "observed/model"
        assert answer.context_usage.label == "25/100 tokens (25.0%)"
        wired.threads.set_thread_configuration(
            wired.registry.require("fixer"), model="configured/model"
        )
        answer = wired.configuration.answer("fixer")
        assert answer.model == "configured/model"
        assert answer.context_usage.used == 25

    def test_runtime_info_rejects_unknown_thread(self, wired):
        with pytest.raises(UnregisteredThreadError):
            wired.agents.observe_runtime_info("ghost", AgentInfo(model="model"))

    def test_list_threads_shape(self, wired):
        rows = {row["name"]: row for row in wired.views.list_threads()}
        assert set(rows) == {"PR111", "fixer"}
        assert rows["fixer"]["status"] == "running"
        assert rows["fixer"]["parent"] == "PR111"
        assert rows["fixer"]["pending"] == 0

    def test_list_active_only(self, wired):
        wired.owners.stop("fixer")
        names = {row["name"] for row in wired.views.list_threads(active_only=True)}
        assert names == {"PR111"}

    @pytest.mark.parametrize("counted", [True, False])
    def test_every_reader_writes_one_thread_record(self, wired, counted):
        """list_threads, thread_detail and who/presence write one thread's record from one parent."""
        views = wired.views
        rows = {
            "detail": views.thread_detail("fixer", include_pending=counted).encoded(),
            "roster": next(row for row in (views.who() if counted else views.presence())
                           if row["name"] == "fixer"),
            **({"list": next(row for row in views.list_threads() if row["name"] == "fixer")}
               if counted else {}),
        }
        record = rows["detail"]
        assert ("pending" in record) is counted
        assert record["is_fork"] is True and record["task"] == "fix auth"
        for row in rows.values():
            assert {key: row[key] for key in record} == record

    def test_thread_detail_fail_closed(self, wired):
        with pytest.raises(UnregisteredThreadError):
            wired.views.thread_detail("ghost")

    def test_heartbeat_marks_running(self, wired):
        wired.owners.stop("fixer")
        wired.threads.heartbeat("fixer")
        assert wired.registry.status("fixer").declared_name == "running"

    def test_attach_session_preserves_declaration_and_updates_runtime(self, wired, tmp_path):
        wired.owners.stop("fixer")
        session_file = tmp_path / "session.jsonl"

        attached = wired.threads.attach_session(
            wired.registry.require("fixer"), str(session_file), process=ProcessIdentity.capture(os.getpid())
        )

        assert attached.tags == frozenset({"auth"})
        assert attached.parent == "PR111"
        assert attached.task == "fix auth"
        assert attached.pid == os.getpid()
        assert attached.session_file == str(session_file.resolve())
        assert wired.registry.status("fixer").declared_name == "running"

    def test_attached_session_model_is_registered_once_not_rescanned_for_answers(
        self, wired, tmp_path, monkeypatch
    ):
        from agent_comms.native_transcript import NativeTranscript

        session_file = tmp_path / "session.jsonl"
        session_file.write_text(
            json.dumps({"type": "model_change", "provider": "test", "modelId": "saved"}) + "\n"
        )
        attached = wired.threads.attach_session((attachment_owner := wired.registry.require("fixer")), str(session_file), process=attachment_owner.process_identity)
        assert attached.model == "test/saved"
        assert wired.registry.require("fixer").model == "test/saved"

        def no_tail(*_args, **_kwargs):
            raise AssertionError("A configuration answer must not scan saved history")

        monkeypatch.setattr(NativeTranscript, "tail", no_tail)
        wired.agents.observe_runtime_info("fixer", AgentInfo(model="runtime/model"))
        assert wired.configuration.answer("fixer").model == "test/saved"
        assert wired.threads.resolve_thread_model("PR111", "default/model") == "default/model"

    def test_release_only_allows_the_calling_thread(self, wired, monkeypatch):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")

        with pytest.raises(RelationViolationError, match="cannot release"):
            wired.owners.release("PR111")

        wired.owners.release("fixer")
        assert wired.registry.status("fixer").declared_name == "stopped"

    def test_stop_current_calling_thread_uses_voluntary_release(self, wired, monkeypatch):
        owner = wired.registry.require("fixer")
        wired.registry.register(
            replace(owner, process_identity=ProcessIdentity.capture(os.getpid()))
        )
        before = wired.registry.snapshot().claimed_generations[owner.name]
        monkeypatch.setenv("AGENT_COMMS_THREAD", owner.name)

        wired.owners.stop(owner.name)

        snapshot = wired.registry.snapshot()
        assert snapshot.statuses[owner.name].declared_name == "stopped"
        assert snapshot.claimed_generations[owner.name] > before

    def test_archive_requires_stopped_thread(self, wired):
        wired.messaging.send("PR111", "fixer", "kept after archive").message.message_id
        with pytest.raises(RelationViolationError, match="no longer stopped"):
            wired.threads.archive("fixer")
        wired.owners.stop("fixer")
        wired.threads.archive("fixer")
        assert wired.registry.status("fixer").declared_name == "archived"
        assert not any(row["name"] == "fixer" for row in wired.views.who())
        assert "fixer" not in wired.registry.active_threads()
        assert [message.body for message in list(wired.views.dm_history_page("PR111", "fixer").messages)] == [
            "kept after archive"
        ]

    def test_rename_self_preserves_routing_history_and_state(self, wired, monkeypatch):
        wired.messaging.send("fixer", "PR111", "before rename").message.message_id
        references = tuple(message.reference for message in wired.bus.inbox("PR111", "fixer"))
        assert wired.messaging.acknowledge("PR111", references) == 1
        wired.agents.set_activity("PR111", ActivityState.WORKING, "renaming")
        wired.agents.observe_runtime_info("PR111", AgentInfo(model="test/model"))
        wired.ledger.merge({"owner": "PR111", "members": ["PR111", "fixer"]}, author="PR111")
        monkeypatch.setenv("AGENT_COMMS_THREAD", "PR111")

        result = wired.threads.rename_self("planner", caller=ThreadCaller("PR111"))

        assert result.previous == "PR111"
        assert result.current == "planner"
        assert result.changed
        assert wired.registry.require("PR111").name == "planner"
        assert wired.registry.require("planner").tags == frozenset({"base"})
        assert wired.registry.require("fixer").parent == "planner"
        assert wired.agents.activity_of("planner", snapshot=wired.registry.snapshot()).detail == "renaming"
        assert wired.agents.agent_info_of("planner").model == "test/model"
        assert wired.ledger.read()["owner"] == "planner"
        assert wired.bus.pending_count("planner", "fixer") == 0

        wired.messaging.send("fixer", "PR111", "old alias routes").message.message_id
        wired.messaging.send("PR111", "fixer", "old process sends canonically").message.message_id
        assert [message.body for message in wired.bus.inbox("planner", "fixer")] == [
            "old alias routes"
        ]
        history = list(wired.views.dm_history_page("PR111", "fixer").messages)
        assert [message.body for message in history] == [
            "before rename",
            "old alias routes",
            "old process sends canonically",
        ]
        assert history[-2].target == "planner"
        assert history[-1].sender == "planner"
        with pytest.raises(RelationViolationError, match="cannot message itself"):
            wired.messaging.send("PR111", "planner", "alias self-DM").message.message_id

    def test_rename_self_rejects_collisions_and_stopped_threads(self, wired, monkeypatch):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")
        with pytest.raises(RelationViolationError, match="already in use"):
            wired.threads.rename_self("PR111", caller=ThreadCaller("fixer"))
        wired.owners.stop("fixer")
        with pytest.raises(RelationViolationError, match="running"):
            wired.threads.rename_self("renamed", caller=ThreadCaller("fixer"))

    def test_rename_self_reclaims_own_alias_but_not_another_owners(self, wired, monkeypatch):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "PR111")
        assert wired.threads.rename_self("pr17", caller=ThreadCaller("PR111")).current == "pr17"
        assert wired.threads.rename_self("PR111", caller=ThreadCaller("PR111")).current == "PR111"
        assert wired.registry.snapshot().aliases == {"pr17": "PR111"}
        assert wired.registry.require("fixer").parent == "PR111"

        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")
        with pytest.raises(RelationViolationError, match="already in use"):
            wired.threads.rename_self("pr17", caller=ThreadCaller("fixer"))

    def test_managed_rename_normalizes_title_and_proves_owner(self, wired):
        wired.registry.declare(
            wired.registry.new_thread(name='generated-7', tags=frozenset({'acp'}), worktree='/tmp/project', process_identity=ProcessIdentity.capture(os.getpid()))
        )

        result = wired.threads.rename_managed_thread(
            "generated-7", "testing 123", owner_pid=os.getpid()
        )

        assert result.previous == "generated-7"
        assert result.current == "testing-123"
        assert wired.registry.require("generated-7").name == "testing-123"
        with pytest.raises(RelationViolationError, match="does not own"):
            wired.threads.rename_managed_thread("testing-123", "wrong", owner_pid=os.getppid())

    def test_managed_rename_disambiguates_duplicate_titles(self, wired):
        wired.registry.declare(
            wired.registry.new_thread(name='testing-123', tags=frozenset(), worktree='/tmp/other', process_identity=ProcessIdentity.capture(os.getppid()))
        )
        wired.registry.declare(
            wired.registry.new_thread(name='generated-7', tags=frozenset({'acp'}), worktree='/tmp/project', process_identity=ProcessIdentity.capture(os.getpid()))
        )

        result = wired.threads.rename_managed_thread("generated-7", "testing 123", owner_pid=os.getpid())

        assert result.current == "testing-123-2"

    def test_managed_rename_reclaims_own_alias(self, wired):
        wired.registry.declare(
            wired.registry.new_thread(name='generated-7', tags=frozenset(), worktree='/tmp/project', process_identity=ProcessIdentity.capture(os.getpid()))
        )
        assert wired.threads.rename_managed_thread(
            "generated-7", "chosen", owner_pid=os.getpid()
        ).changed
        result = wired.threads.rename_managed_thread("chosen", "generated-7", owner_pid=os.getpid())
        assert (result.previous, result.current, result.changed) == ("chosen", "generated-7", True)
        assert wired.registry.snapshot().aliases["chosen"] == "generated-7"
        assert "generated-7" not in wired.registry.snapshot().aliases

    def test_old_alias_cannot_be_reused(self, wired, monkeypatch):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")
        wired.threads.rename_self("reviewer", caller=ThreadCaller("fixer"))
        with pytest.raises(RelationViolationError, match="permanent alias"):
            wired.registry.register(wired.registry.new_thread(name='fixer', tags=frozenset(), worktree='/tmp/other'))

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="uses /proc")
    def test_stop_terminates_real_process(self, wired):
        env = dict(
            __import__("os").environ,
            AGENT_COMMS_THREAD="live-process",
            AGENT_COMMS_ROOT=str(wired.root),
        )
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            env=env,
        )
        wired.registry.declare(
            wired.registry.new_thread(name='live-process', tags=frozenset(), worktree='/tmp', process_identity=ProcessIdentity.capture(process.pid))
        )
        try:
            wired.owners.stop("live-process")
            assert process.wait(timeout=5) == -__import__("signal").SIGTERM
            assert wired.registry.status("live-process").declared_name == "stopped"
        finally:
            if process.poll() is None:
                process.kill()


class TestFork:
    def test_fork_requires_parent_session(self, wired):
        with pytest.raises(ValueError, match="Canonical saved session required"):
            wired.threads.create(ForkSpec.from_task(name="child", parent="PR111", project="/project", model="test/model", thinking_level=HighThinkingLevel, task="t"))

    def test_fork_fail_closed_unknown_parent(self, wired):
        with pytest.raises(UnregisteredThreadError):
            wired.threads.create(ForkSpec.from_task(name="child", parent="ghost", project="/project", model="test/model", thinking_level=HighThinkingLevel, task="t"))


class TestLedgerOps:
    def test_recent_transcript_is_bounded_and_ordered(self, wired, tmp_path):
        import json

        from agent_comms.pi_native_backend import PiSessionFile

        path = tmp_path / "large-session.jsonl"
        with path.open("w") as stream:
            stream.write(
                json.dumps({"type": "model_change", "provider": "test", "modelId": "one"}) + "\n"
            )
            # A giant record crossing the tail boundary must never trigger an
            # unbounded readline or hide the newest small records.
            stream.write(
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "x" * 2_000_000}],
                        },
                    }
                )
                + "\n"
            )
            for index in range(100):
                stream.write(
                    json.dumps(
                        {
                            "type": "message",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": str(index)}],
                            },
                        }
                    )
                    + "\n"
                )
        wired.registry.declare(
            wired.registry.new_thread(name='large', tags=frozenset(), worktree=str(tmp_path), session_file=str(path))
        )
        page = wired.transcripts.thread_transcript_page("large")
        assert page.has_older
        assert [event.text for event in page.events] == [str(index) for index in range(80, 100)]
        assert PiSessionFile(str(path)).model_choice == ("test", "one")

    def test_merge_requires_registered_author(self, wired):
        with pytest.raises(UnregisteredThreadError, match="Author"):
            wired.ledger.merge({"k": "v"}, author="ghost")

    def test_merge_records_author(self, wired):
        wired.ledger.merge({"k": "v"}, author="PR111")
        assert wired.ledger.read()["last_updated_by"] == "PR111"


class TestPollAndWire:
    def test_poll_snapshot(self, wired):
        wired.messaging.send("PR111", "fixer", "hello").message.message_id
        snap = wired.views.poll("fixer").encoded()
        assert snap["thread"]["name"] == "fixer"
        assert len(snap["inbox"]) == 1
        assert [peer["name"] for peer in snap["peers"]] == ["PR111"]
        assert snap["peers"][0]["activity"] == "idle"
        assert "ledger" in snap

    def test_poll_current_thread_from_env(self, wired, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")
        monkeypatch.setenv("PI_WORKTREE", str(tmp_path))
        snap = wired.views.poll().encoded()
        assert snap["thread"]["name"] == "fixer"

    def test_adopt_current_registers_from_env(self, comms, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "me")
        monkeypatch.setenv("PI_WORKTREE", str(tmp_path))
        thread = comms.threads.adopt_current()
        assert thread.name == "me"
        assert comms.registry.status("me").declared_name == "running"

    def test_wire_defaults_to_agent_comms_dir(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AGENT_COMMS_ROOT", raising=False)
        from agent_comms.comms import wire as wire_fn

        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / str(p).lstrip("~")))
        comms = wire_fn(None)
        assert comms.root == tmp_path / ".agent-comms"

    def test_wire_env_root(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_COMMS_ROOT", str(tmp_path / "custom"))
        comms = wire(None)
        assert comms.root == tmp_path / "custom"

    def test_runtime_files_created_on_demand(self, tmp_path):
        comms = wire(tmp_path / "fresh")
        comms.registry.declare(comms.registry.new_thread(name='a', tags=frozenset(), worktree='/wt'))
        assert (tmp_path / "fresh" / "registry.json").exists()


class TestCrossWireIsolation:
    def test_two_wires_do_not_leak(self, tmp_path):
        a = wire(tmp_path / "a")
        b = wire(tmp_path / "b")
        a.registry.declare(a.registry.new_thread(name='only-in-a', tags=frozenset(), worktree='/wt'))
        assert "only-in-a" in a.registry
        with pytest.raises(UnregisteredThreadError):
            b.registry.require("only-in-a")


class TestReDeclarationPreservesProvenance:
    """Regression: a child that re-registers without tag env must keep
    the tags its fork declared, or it silently loses channel access."""

    def test_empty_tags_inherit_previous(self, wired):
        wired.registry.declare(wired.registry.new_thread(name='fixer', tags=frozenset(), worktree='/tmp/wt1'))
        assert wired.registry.require("fixer").tags == frozenset({"auth"})

    def test_explicit_tags_replace_previous(self, wired):
        wired.registry.declare(wired.registry.new_thread(name='fixer', tags=frozenset({'docs'}), worktree='/tmp/wt1'))
        assert wired.registry.require("fixer").tags == frozenset({"docs"})

    def test_missing_session_file_inherits_previous(self, wired, tmp_path):
        wired.registry.declare(
            wired.registry.new_thread(name='PR111', tags=frozenset({'base'}), worktree='/tmp/wt1', session_file=str(tmp_path / 's.json'))
        )
        wired.registry.declare(wired.registry.new_thread(name='PR111', tags=frozenset({'base'}), worktree='/tmp/wt1'))
        assert wired.registry.require("PR111").session_file == str(tmp_path / "s.json")

    def test_fresh_declaration_is_unaffected(self, wired):
        wired.registry.declare(wired.registry.new_thread(name='fresh', tags=frozenset(), worktree='/wt'))
        assert wired.registry.require("fresh").tags == frozenset()

    def test_tagless_child_keeps_channel_after_reregister(self, wired):
        wired.registry.declare(wired.registry.new_thread(name='fixer', tags=frozenset(), worktree='/tmp/wt1'))
        wired.messaging.send("PR111", "#auth", "only tagged fixer sees this").message.message_id
        assert wired.bus.pending_count("fixer") == 1

