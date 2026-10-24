
from conftest import transcript_records
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from pydantic import JsonValue

import pytest

from agent_comms.session_file import RegistrySessionLocation
from agent_comms.native_entries import CompactionEntry, ModelChangeEntry
from agent_comms.channel_targets import ChannelTarget, CommandTarget, ThreadTarget
from agent_comms.channels import AnyChannel
from agent_comms.activity import ActivityState
from agent_comms.agent_events import AgentInfo, ContextUsageObservation
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms, wire
from agent_comms.field_codec import FieldCodec
from agent_comms.errors import MessageAcknowledgmentError, RelationViolationError, UnregisteredThreadError
from agent_comms.history_views import CallingThreadTarget, NamedThreadTarget
from agent_comms.launch_runtime import CallingThreadUnavailable, ThreadLaunch
from agent_comms.message_page import PageBounds
from agent_comms.messages import QuestionKind
from agent_comms.pi_counts import PiCount
from agent_comms.pi_vocabulary import HighThinkingLevel
from agent_comms.presentation import CountedInbox, CountedRead, UncountedRead
from agent_comms.context_reading import ContextUsage
from agent_comms.transcript_events import AssistantTranscript, NoticeTranscript, ToolStartTranscript, ToolEndTranscript
from agent_comms.read_basis import NamedViewer
from agent_comms.thread_management import ForkSpec
from agent_comms.tool_caller import ThreadCaller
from agent_comms.threads import ChosenModel, NextTurnConfiguration
from agent_comms.thread_text import ForkedParent, GivenThreadText
from agent_comms.thread_model import DeclaredModel
from agent_comms.thread_status import HeartbeatThreadStatus
from agent_comms.threads import Thread


def test_calling_thread_is_declared_by_owner_launch_environment() -> None:
    assert ThreadLaunch.current({"AGENT_COMMS_THREAD": "owner"}).name == "owner"
    with pytest.raises(CallingThreadUnavailable):
        ThreadLaunch.current({})
    assert not ThreadLaunch.called_by("owner", lambda caller: caller, environment={})


class TestMessaging:
    def test_send_routes_through_bus(self, wired: Comms) -> None:
        mid = wired.messaging.send("PR111", "fixer", "hello", QuestionKind).message.message_id
        assert isinstance(mid, str)
        assert wired.bus.pending_count("fixer") == 1

    def test_send_fail_closed_unknown_sender(self, wired: Comms) -> None:
        with pytest.raises(UnregisteredThreadError):
            wired.messaging.send("ghost", "fixer", "hello").message.message_id

    def test_broadcast_reaches_all_peers(self, wired: Comms) -> None:
        wired.messaging.send("PR111", "#all", "green").message.message_id
        assert wired.bus.pending_count("fixer") == 1

    def test_ack_clears_inbox(self, wired: Comms) -> None:
        wired.messaging.send("PR111", "fixer", "hello").message.message_id
        messages = wired.bus.inbox("fixer").page.messages
        references = tuple(message.reference for message in messages)
        assert wired.messaging.acknowledge("fixer", references) == 1
        assert wired.bus.pending_count("fixer") == 0
        with pytest.raises(MessageAcknowledgmentError) as caught:
            wired.messaging.acknowledge("fixer", references)
        assert caught.value.references == references
        assert wired.messaging.acknowledge("fixer", ()) == 0

    def test_ack_refuses_partial_selection_before_changing_reads(self, wired: Comms) -> None:
        incoming = wired.messaging.send("PR111", "fixer", "incoming").message
        outgoing = wired.messaging.send("fixer", "PR111", "not delivered to fixer").message
        before = wired.bus.reads.read()
        with pytest.raises(MessageAcknowledgmentError) as caught:
            wired.messaging.acknowledge("fixer", (incoming.reference, outgoing.reference))
        assert caught.value.recipient == "fixer"
        assert caught.value.references == (outgoing.reference,)
        assert wired.bus.reads.read() == before
        assert [message.reference for message in wired.bus.inbox("fixer").page.messages] == [incoming.reference]

    def test_ack_names_an_invalid_original_reference(self, wired: Comms) -> None:
        incoming = wired.messaging.send("PR111", "fixer", "incoming").message
        invalid = replace(incoming.reference, message_id="not-the-original")
        with pytest.raises(MessageAcknowledgmentError) as caught:
            wired.messaging.acknowledge("fixer", (invalid,))
        assert caught.value.references == (invalid,)
        assert [message.reference for message in wired.bus.inbox("fixer").page.messages] == [incoming.reference]

    def test_scoped_ack_only_clears_selected_conversation(self, wired: Comms) -> None:
        wired.messaging.send("PR111", "fixer", "direct").message.message_id
        wired.messaging.send("PR111", "#all", "global").message.message_id

        references = tuple(message.reference for message in wired.bus.inbox("fixer", CommandTarget.named("PR111")).page.messages)
        assert wired.messaging.acknowledge("fixer", references) == 1
        assert wired.bus.pending_count("fixer", CommandTarget.named("PR111")) == 0
        assert wired.bus.pending_count("fixer", CommandTarget.named("#all")) == 1
        assert wired.bus.pending_count("fixer") == 1

    def test_each_target_member_reads_and_counts_the_same_messages(self, wired: Comms) -> None:
        wired.messaging.send("PR111", "fixer", "direct")
        wired.messaging.send("PR111", "#all", "global")

        expected = {AnyChannel.target(): {"direct", "global"}, ThreadTarget("PR111"): {"direct"},
                    ChannelTarget("#all"): {"global"}}
        for target, bodies in expected.items():
            assert {message.body for message in wired.bus.inbox("fixer", target).page.messages} == bodies
            assert wired.bus.pending_count("fixer", target) == len(bodies)

    def test_scoped_channel_ack_does_not_clear_dm(self, wired: Comms) -> None:
        wired.messaging.send("PR111", "fixer", "direct").message.message_id
        wired.messaging.send("PR111", "#all", "global").message.message_id

        references = tuple(message.reference for message in wired.bus.inbox("fixer", CommandTarget.named("#all")).page.messages)
        assert wired.messaging.acknowledge("fixer", references) == 1
        assert wired.bus.pending_count("fixer", CommandTarget.named("#all")) == 0
        assert wired.bus.pending_count("fixer", CommandTarget.named("PR111")) == 1
        assert wired.bus.pending_count("fixer") == 1

    def test_inbox_order_follows_seq(self, wired: Comms) -> None:
        for i in range(5):
            wired.messaging.send("PR111", "fixer", f"m{i}").message.message_id
        bodies = [m.body for m in wired.bus.inbox("fixer").page.messages]
        assert bodies == [f"m{i}" for i in range(5)]

    def test_opened_pending_counts_groups_all_conversations_in_one_result(self, wired: Comms) -> None:
        wired.messaging.send("PR111", "fixer", "direct one").message.message_id
        wired.messaging.send("PR111", "fixer", "direct two").message.message_id
        wired.messaging.send("PR111", "#all", "global").message.message_id

        with wired.bus.snapshot(viewer=NamedViewer("fixer")) as (basis, source):
            assert wired.bus.pending_counts_opened(source, basis) == {"PR111": 2, "#all": 1}
        references = tuple(message.reference for message in wired.bus.inbox("fixer", CommandTarget.named("PR111")).page.messages)
        wired.messaging.acknowledge("fixer", references)
        with wired.bus.snapshot(viewer=NamedViewer("fixer")) as (basis, source):
            assert wired.bus.pending_counts_opened(source, basis) == {"#all": 1}

    def test_shared_history_page_contract(self, wired: Comms) -> None:
        for index in range(5):
            wired.messaging.send("PR111", "#all", f"m{index}").message.message_id

        page = wired.views.history_page(CommandTarget.named("#all"), PageBounds(limit=2))
        assert [message.body for message in page.messages] == ["m3", "m4"]
        assert page.has_older
        assert wired.bus.bus.revision() == 5


class TestThreadOps:
    def test_thread_transcript_normalizes_persisted_pi_events(self, wired: Comms, tmp_path: Path) -> None:
        session_file = tmp_path / "session.jsonl"
        records = [
            transcript_records.message({
                    "role": "user",
                    "content": [{"type": "text", "text": "demo request"}],
                }),
            transcript_records.message({
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
                }),
            transcript_records.message({
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "comms_send",
                    "content": [{"type": "text", "text": "sent"}],
                    "isError": False,
                }),
            transcript_records.message({
                    "role": "assistant",
                    "content": [{"type": "text", "text": "demo complete"}],
                }),
        ]
        session_file.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        wired.registry.declare(
            Thread(name='transcript-thread', tags=frozenset(), worktree=str(tmp_path), session_file=RegistrySessionLocation.from_path(str(session_file), wired.root))
        )

        events = wired.transcripts.thread_transcript_page("transcript-thread").events

        assert [event.declared_name for event in events] == [
            "user",
            "thinking",
            "tool_start",
            "tool_end",
            "assistant",
        ]
        start, end = events[2:4]
        assert isinstance(start, ToolStartTranscript)
        assert isinstance(end, ToolEndTranscript)
        assert start.raw_input == {"to": "child"}
        assert end.text == "sent"
        row = FieldCodec.decode(dict[str, JsonValue], next(row.to_wire() for row in wired.views.presence() if row.thread.name == "transcript-thread"))
        assert row["resumable"] is True

    def test_thread_transcript_includes_saved_compaction_summary(self, wired: Comms, tmp_path: Path) -> None:
        session_file = tmp_path / "compacted.jsonl"
        session_file.write_text(
            json.dumps(
                transcript_records.entry(CompactionEntry, summary="Important decisions and remaining work.").to_wire()
            )
            + "\n"
        )
        wired.registry.declare(
            Thread(name='compacted-thread', tags=frozenset(), worktree=str(tmp_path), session_file=RegistrySessionLocation.from_path(str(session_file), wired.root))
        )

        events = wired.transcripts.thread_transcript_page("compacted-thread").events

        assert len(events) == 1
        notice = events[0]
        assert notice.declared_name == "notice"
        assert isinstance(notice, NoticeTranscript)
        assert "Important decisions" in notice.text

    def test_claimed_thread_inbox_starts_at_its_creation(self, wired: Comms) -> None:
        wired.messaging.send("PR111", "#all", "before claim").message.message_id
        claimed = wired.threads.claim_thread(
            "viewer",
            tags=frozenset({"acp"}),
            worktree="/tmp/project",
        )
        assert wired.bus.inbox(claimed.name).page.messages == ()
        wired.messaging.send("PR111", "#all", "after claim").message.message_id
        assert [message.body for message in wired.bus.inbox(claimed.name).page.messages] == ["after claim"]

    def test_presence_uses_configured_model_with_runtime_context(self, wired: Comms) -> None:
        wired.threads.set_thread_configuration(
            wired.registry.require("fixer"), NextTurnConfiguration(model=ChosenModel(DeclaredModel('configured/model')))
        )
        wired.agents.observe_runtime_info("fixer", ContextUsageObservation(context_used=PiCount.from_wire(25)))
        wired.agents.observe_runtime_info(
            "fixer", AgentInfo(model=DeclaredModel('observed/model'), context_size=PiCount.from_wire(100))
        )
        row = FieldCodec.decode(dict[str, JsonValue], next(row.to_wire() for row in wired.views.who() if row.thread.name == "fixer"))
        assert row["model"] == "configured/model"
        assert (row["context_used"], row["context_size"]) == (25, 100)
        assert wired.agents.agent_info_of("fixer").context_used == PiCount.from_wire(25)

    def test_configuration_names_the_resolved_model(self, wired: Comms) -> None:
        wired.agents.observe_runtime_info("fixer", ContextUsageObservation(context_used=PiCount.from_wire(25)))
        wired.agents.observe_runtime_info("fixer", AgentInfo(model=DeclaredModel('observed/model'), context_size=PiCount.from_wire(100)))
        answer = wired.configuration.answer("fixer")
        assert answer.model == "observed/model"
        assert answer.context_usage == ContextUsage(25, 100)
        wired.threads.set_thread_configuration(
            wired.registry.require("fixer"), NextTurnConfiguration(model=ChosenModel(DeclaredModel('configured/model')))
        )
        answer = wired.configuration.answer("fixer")
        assert answer.model == "configured/model"
        usage = answer.context_usage
        assert isinstance(usage, ContextUsage)
        assert usage.used == 25

    def test_runtime_info_rejects_unknown_thread(self, wired: Comms) -> None:
        with pytest.raises(UnregisteredThreadError):
            wired.agents.observe_runtime_info("ghost", AgentInfo(model=DeclaredModel('model')))

    def test_list_threads_shape(self, wired: Comms) -> None:
        rows = {row.thread.name: row for row in wired.views.list_threads()}
        assert set(rows) == {"PR111", "fixer"}
        assert isinstance(rows["fixer"].status, HeartbeatThreadStatus)
        assert rows["fixer"].thread.parent == ForkedParent("PR111")
        assert rows["fixer"].pending == CountedInbox(0)

    def test_list_active_only(self, wired: Comms) -> None:
        wired.owners.stop("fixer")
        names = {row.thread.name for row in wired.views.list_threads(active_only=True)}
        assert names == {"PR111"}

    @pytest.mark.parametrize("counted", [True, False])
    def test_every_reader_writes_one_thread_record(self, wired: Comms, counted: bool) -> None:
        """list_threads, thread_detail and who/presence write one thread's record from one parent."""
        views = wired.views
        rows = {
            "detail": views.thread_detail(
                "fixer", CountedRead() if counted else UncountedRead()).to_wire(),
            **({"list": next(row.to_wire() for row in views.list_threads() if row.thread.name == "fixer")}
               if counted else {
                   reader: next(row.to_wire() for row in rows() if row.thread.name == "fixer")
                   for reader, rows in (("who", views.who), ("presence", views.presence))}),
        }
        records = {name: FieldCodec.decode(dict[str, JsonValue], row) for name, row in rows.items()}
        record = records["detail"]
        assert ("pending" in record) is counted
        assert record["is_fork"] is True and record["task"] == "fix auth"
        for row in records.values():
            assert {key: row[key] for key in record} == record

    def test_thread_detail_fail_closed(self, wired: Comms) -> None:
        with pytest.raises(UnregisteredThreadError):
            wired.views.thread_detail("ghost", CountedRead())

    def test_heartbeat_marks_running(self, wired: Comms) -> None:
        wired.owners.stop("fixer")
        wired.threads.heartbeat("fixer")
        assert isinstance(wired.registry.status("fixer"), HeartbeatThreadStatus)

    def test_attach_session_preserves_declaration_and_updates_runtime(self, wired: Comms, tmp_path: Path) -> None:
        wired.owners.stop("fixer")
        session_file = tmp_path / "session.jsonl"

        attached = wired.threads.attach_session(
            wired.registry.require("fixer"), str(session_file), process=ProcessIdentity.capture(os.getpid())
        )

        assert attached.tags == frozenset({"auth"})
        assert attached.parent == ForkedParent("PR111")
        assert attached.task == GivenThreadText("fix auth")
        assert attached.process_identity == ProcessIdentity.capture(os.getpid())
        assert attached.session_file == RegistrySessionLocation.from_path(str(session_file.resolve()), wired.root)
        assert wired.registry.status("fixer").declared_name == "running"


    def test_release_only_allows_the_calling_thread(self, wired: Comms, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")

        with pytest.raises(RelationViolationError):
            wired.owners.release("PR111")

        wired.owners.release("fixer")
        assert wired.registry.status("fixer").declared_name == "stopped"

    def test_stop_current_calling_thread_uses_voluntary_release(self, wired: Comms, monkeypatch: pytest.MonkeyPatch) -> None:
        owner = wired.registry.require("fixer")
        wired.registry.register(
            replace(owner, process_identity=ProcessIdentity.capture(os.getpid()))
        )
        before = wired.registry.snapshot().claim_generation(owner.name)
        monkeypatch.setenv("AGENT_COMMS_THREAD", owner.name)

        wired.owners.stop(owner.name)

        snapshot = wired.registry.snapshot()
        assert snapshot.statuses[owner.name].declared_name == "stopped"
        assert snapshot.claim_generation(owner.name) > before

    def test_archive_requires_stopped_thread(self, wired: Comms) -> None:
        wired.messaging.send("PR111", "fixer", "kept after archive").message.message_id
        with pytest.raises(RelationViolationError):
            wired.threads.archive("fixer")
        wired.owners.stop("fixer")
        wired.threads.archive("fixer")
        assert wired.registry.status("fixer").declared_name == "archived"
        assert not any(row.thread.name == "fixer" for row in wired.views.who())
        assert "fixer" not in wired.registry.active_threads()
        assert [message.body for message in list(wired.views.history_page(CommandTarget.named("fixer"), viewer=NamedViewer("PR111")).messages)] == [
            "kept after archive"
        ]

    def test_rename_self_preserves_routing_history_and_state(self, wired: Comms, monkeypatch: pytest.MonkeyPatch) -> None:
        wired.messaging.send("fixer", "PR111", "before rename").message.message_id
        references = tuple(message.reference for message in wired.bus.inbox("PR111", CommandTarget.named("fixer")).page.messages)
        assert wired.messaging.acknowledge("PR111", references) == 1
        wired.agents.set_activity("PR111", ActivityState.WORKING, "renaming")
        wired.agents.observe_runtime_info("PR111", AgentInfo(model=DeclaredModel('test/model')))
        wired.ledger.merge({"owner": "PR111", "members": ["PR111", "fixer"]}, author="PR111")
        monkeypatch.setenv("AGENT_COMMS_THREAD", "PR111")

        result = wired.threads.rename_self("planner", caller=ThreadCaller("PR111"))

        assert result.previous == "PR111"
        assert result.current == "planner"
        assert result.changed
        assert wired.registry.require("PR111").name == "planner"
        assert wired.registry.require("planner").tags == frozenset({"base"})
        assert wired.registry.require("fixer").parent == ForkedParent("planner")
        assert wired.agents.activity.current_for(("planner",), active=frozenset({"planner"}))["planner"].detail == "renaming"
        assert wired.agents.agent_info_of("planner").model == DeclaredModel("test/model")
        ledger = FieldCodec.encode(wired.ledger.read())
        assert isinstance(ledger, dict)
        assert ledger["owner"] == "planner"
        assert wired.bus.pending_count("planner", CommandTarget.named("fixer")) == 0

        wired.messaging.send("fixer", "PR111", "old alias routes").message.message_id
        wired.messaging.send("PR111", "fixer", "old process sends canonically").message.message_id
        assert [message.body for message in wired.bus.inbox("planner", CommandTarget.named("fixer")).page.messages] == [
            "old alias routes"
        ]
        history = list(wired.views.history_page(CommandTarget.named("fixer"), viewer=NamedViewer("PR111")).messages)
        assert [message.body for message in history] == [
            "before rename",
            "old alias routes",
            "old process sends canonically",
        ]
        assert history[-2].target == "planner"
        assert history[-1].sender == "planner"
        with pytest.raises(RelationViolationError):
            wired.messaging.send("PR111", "planner", "alias self-DM").message.message_id

    def test_rename_self_rejects_collisions_and_stopped_threads(self, wired: Comms, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")
        with pytest.raises(RelationViolationError):
            wired.threads.rename_self("PR111", caller=ThreadCaller("fixer"))
        wired.owners.stop("fixer")
        with pytest.raises(RelationViolationError):
            wired.threads.rename_self("renamed", caller=ThreadCaller("fixer"))

    def test_rename_self_reclaims_own_alias_but_not_another_owners(self, wired: Comms, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_COMMS_THREAD", "PR111")
        assert wired.threads.rename_self("pr17", caller=ThreadCaller("PR111")).current == "pr17"
        assert wired.threads.rename_self("PR111", caller=ThreadCaller("PR111")).current == "PR111"
        assert wired.registry.snapshot().aliases == {"pr17": "PR111"}
        assert wired.registry.require("fixer").parent == ForkedParent("PR111")

        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")
        with pytest.raises(RelationViolationError):
            wired.threads.rename_self("pr17", caller=ThreadCaller("fixer"))

    def test_managed_rename_normalizes_title_and_proves_owner(self, wired: Comms) -> None:
        wired.registry.declare(
            Thread(name='generated-7', tags=frozenset({'acp'}), worktree='/tmp/project', process_identity=ProcessIdentity.capture(os.getpid()))
        )

        result = wired.threads.rename_managed_thread(
            "generated-7", "testing 123", owner=ProcessIdentity.capture(os.getpid())
        )

        assert result.previous == "generated-7"
        assert result.current == "testing-123"
        assert wired.registry.require("generated-7").name == "testing-123"
        with pytest.raises(RelationViolationError):
            wired.threads.rename_managed_thread("testing-123", "wrong", owner=ProcessIdentity.capture(os.getppid()))

    def test_managed_rename_disambiguates_duplicate_titles(self, wired: Comms) -> None:
        wired.registry.declare(
            Thread(name='testing-123', tags=frozenset(), worktree='/tmp/other', process_identity=ProcessIdentity.capture(os.getppid()))
        )
        wired.registry.declare(
            Thread(name='generated-7', tags=frozenset({'acp'}), worktree='/tmp/project', process_identity=ProcessIdentity.capture(os.getpid()))
        )

        result = wired.threads.rename_managed_thread("generated-7", "testing 123", owner=ProcessIdentity.capture(os.getpid()))

        assert result.current == "testing-123-2"

    def test_managed_rename_reclaims_own_alias(self, wired: Comms) -> None:
        wired.registry.declare(
            Thread(name='generated-7', tags=frozenset(), worktree='/tmp/project', process_identity=ProcessIdentity.capture(os.getpid()))
        )
        assert wired.threads.rename_managed_thread(
            "generated-7", "chosen", owner=ProcessIdentity.capture(os.getpid())
        ).changed
        result = wired.threads.rename_managed_thread("chosen", "generated-7", owner=ProcessIdentity.capture(os.getpid()))
        assert (result.previous, result.current, result.changed) == ("chosen", "generated-7", True)
        assert wired.registry.snapshot().aliases["chosen"] == "generated-7"
        assert "generated-7" not in wired.registry.snapshot().aliases

    def test_old_alias_cannot_be_reused(self, wired: Comms, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")
        wired.threads.rename_self("reviewer", caller=ThreadCaller("fixer"))
        with pytest.raises(RelationViolationError):
            wired.registry.register(Thread(name='fixer', tags=frozenset(), worktree='/tmp/other'))

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="uses /proc")
    def test_stop_terminates_real_process(self, wired: Comms) -> None:
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
            Thread(name='live-process', tags=frozenset(), worktree='/tmp', process_identity=ProcessIdentity.capture(process.pid))
        )
        try:
            wired.owners.stop("live-process")
            assert process.wait(timeout=5) == -__import__("signal").SIGTERM
            assert wired.registry.status("live-process").declared_name == "stopped"
        finally:
            if process.poll() is None:
                process.kill()


class TestFork:
    def test_fork_requires_parent_session(self, wired: Comms) -> None:
        with pytest.raises(ValueError):
            wired.threads.create(ForkSpec.from_task(name="child", parent="PR111", project="/project", model=DeclaredModel('test/model'), thinking_level=HighThinkingLevel, task="t"))

    def test_fork_fail_closed_unknown_parent(self, wired: Comms) -> None:
        with pytest.raises(UnregisteredThreadError):
            wired.threads.create(ForkSpec.from_task(name="child", parent="ghost", project="/project", model=DeclaredModel('test/model'), thinking_level=HighThinkingLevel, task="t"))


class TestLedgerOps:
    def test_recent_transcript_is_bounded_and_ordered(self, wired: Comms, tmp_path: Path) -> None:
        import json

        from agent_comms.pi_native_backend import PiSessionFile

        path = tmp_path / "large-session.jsonl"
        with path.open("w") as stream:
            stream.write(
                json.dumps(transcript_records.entry(ModelChangeEntry, provider="test", model_id="one").to_wire()) + "\n"
            )
            # A giant record crossing the tail boundary must never trigger an
            # unbounded readline or hide the newest small records.
            stream.write(
                json.dumps(
                    transcript_records.message({
                            "role": "assistant",
                            "content": [{"type": "text", "text": "x" * 2_000_000}],
                        })
                )
                + "\n"
            )
            for index in range(100):
                stream.write(
                    json.dumps(
                        transcript_records.message({
                                "role": "assistant",
                                "content": [{"type": "text", "text": str(index)}],
                            })
                    )
                    + "\n"
                )
        wired.registry.declare(
            Thread(name='large', tags=frozenset(), worktree=str(tmp_path), session_file=RegistrySessionLocation.from_path(str(path), wired.root))
        )
        page = wired.transcripts.thread_transcript_page("large")
        assert page.has_older
        texts: list[str] = []
        for event in page.events:
            assert isinstance(event, AssistantTranscript)
            texts.append(event.text)
        assert texts == [str(index) for index in range(80, 100)]
        assert PiSessionFile(str(path)).model_choice == ("test", "one")

    def test_merge_requires_registered_author(self, wired: Comms) -> None:
        with pytest.raises(UnregisteredThreadError):
            wired.ledger.merge({"k": "v"}, author="ghost")

    def test_merge_records_author(self, wired: Comms) -> None:
        wired.ledger.merge({"k": "v"}, author="PR111")
        ledger = FieldCodec.encode(wired.ledger.read())
        assert isinstance(ledger, dict)
        assert ledger["last_updated_by"] == "PR111"


class TestPollAndWire:
    def test_poll_snapshot(self, wired: Comms) -> None:
        wired.messaging.send("PR111", "fixer", "hello").message.message_id
        snap = FieldCodec.decode(dict[str, JsonValue], wired.views.poll(NamedThreadTarget("fixer")).to_wire())
        thread = FieldCodec.decode(dict[str, JsonValue], snap["thread"])
        inbox = snap["inbox"]
        assert isinstance(inbox, list)
        peers = FieldCodec.decode(list[dict[str, JsonValue]], snap["peers"])
        assert thread["name"] == "fixer"
        assert len(inbox) == 1
        assert [peer["name"] for peer in peers] == ["PR111"]
        assert peers[0]["activity"] == "idle"
        assert "ledger" in snap

    def test_poll_current_thread_from_env(self, wired: Comms, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")
        monkeypatch.setenv("PI_WORKTREE", str(tmp_path))
        snap = FieldCodec.decode(dict[str, JsonValue], wired.views.poll(CallingThreadTarget()).to_wire())
        thread = FieldCodec.decode(dict[str, JsonValue], snap["thread"])
        assert thread["name"] == "fixer"

    def test_adopt_current_registers_from_env(self, comms: Comms, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("AGENT_COMMS_THREAD", "me")
        monkeypatch.setenv("PI_WORKTREE", str(tmp_path))
        thread = comms.threads.adopt_current()
        assert thread.name == "me"
        assert comms.registry.status("me").declared_name == "running"

    def test_runtime_files_created_on_demand(self, tmp_path: Path) -> None:
        comms = wire(tmp_path / "fresh")
        comms.registry.declare(Thread(name='a', tags=frozenset(), worktree='/wt'))
        assert (tmp_path / "fresh" / "registry.json").exists()


class TestCrossWireIsolation:
    def test_two_wires_do_not_leak(self, tmp_path: Path) -> None:
        a = wire(tmp_path / "a")
        b = wire(tmp_path / "b")
        a.registry.declare(Thread(name='only-in-a', tags=frozenset(), worktree='/wt'))
        assert "only-in-a" in a.registry
        with pytest.raises(UnregisteredThreadError):
            b.registry.require("only-in-a")


class TestReDeclarationPreservesProvenance:
    """Regression: a child that re-registers without tag env must keep
    the tags its fork declared, or it silently loses channel access."""

    def test_empty_tags_inherit_previous(self, wired: Comms) -> None:
        wired.registry.declare(Thread(name='fixer', tags=frozenset(), worktree='/tmp/wt1'))
        assert wired.registry.require("fixer").tags == frozenset({"auth"})

    def test_explicit_tags_replace_previous(self, wired: Comms) -> None:
        wired.registry.declare(Thread(name='fixer', tags=frozenset({'docs'}), worktree='/tmp/wt1'))
        assert wired.registry.require("fixer").tags == frozenset({"docs"})

    def test_missing_session_file_inherits_previous(self, wired: Comms, tmp_path: Path) -> None:
        wired.registry.declare(
            Thread(name='PR111', tags=frozenset({'base'}), worktree='/tmp/wt1', session_file=RegistrySessionLocation.from_path(str(tmp_path / 's.json'), wired.root))
        )
        wired.registry.declare(Thread(name='PR111', tags=frozenset({'base'}), worktree='/tmp/wt1'))
        assert wired.registry.require("PR111").session_file == RegistrySessionLocation.from_path(str(tmp_path / "s.json"), wired.root)

    def test_fresh_declaration_is_unaffected(self, wired: Comms) -> None:
        wired.registry.declare(Thread(name='fresh', tags=frozenset(), worktree='/wt'))
        assert wired.registry.require("fresh").tags == frozenset()

    def test_tagless_child_keeps_channel_after_reregister(self, wired: Comms) -> None:
        wired.registry.declare(Thread(name='fixer', tags=frozenset(), worktree='/tmp/wt1'))
        wired.messaging.send("PR111", "#auth", "only tagged fixer sees this").message.message_id
        assert wired.bus.pending_count("fixer") == 1

