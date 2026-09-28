import json
import os
import subprocess
import sys

import pytest

from agent_comms.activity import ActivityState
from agent_comms.comms import wire
from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.messages import MessageType
from agent_comms.thread_management import ForkSpec
from agent_comms.threads import Thread


class TestMessaging:
    def test_send_routes_through_bus(self, wired):
        mid = wired.messaging.send("PR111", "fixer", "hello", MessageType.QUESTION)
        assert isinstance(mid, str)
        assert wired.bus.pending_count("fixer") == 1

    def test_send_fail_closed_unknown_sender(self, wired):
        with pytest.raises(UnregisteredThreadError, match="Sender"):
            wired.messaging.send("ghost", "fixer", "hello")

    def test_broadcast_reaches_all_peers(self, wired):
        wired.messaging.broadcast("PR111", "green")
        assert wired.bus.pending_count("fixer") == 1

    def test_ack_clears_inbox(self, wired):
        wired.messaging.send("PR111", "fixer", "hello")
        assert wired.messaging.acknowledge("fixer") == 1
        assert wired.bus.pending_count("fixer") == 0
        assert wired.messaging.acknowledge("fixer") == 0

    def test_scoped_ack_only_clears_selected_conversation(self, wired):
        wired.messaging.send("PR111", "fixer", "direct")
        wired.messaging.send("PR111", "#all", "global")

        assert wired.messaging.acknowledge("fixer", "PR111") == 1
        assert wired.bus.pending_count("fixer", "PR111") == 0
        assert wired.bus.pending_count("fixer", "#all") == 1
        assert wired.bus.pending_count("fixer") == 1

    def test_scoped_channel_ack_does_not_clear_dm(self, wired):
        wired.messaging.send("PR111", "fixer", "direct")
        wired.messaging.send("PR111", "#all", "global")

        assert wired.messaging.acknowledge("fixer", "#all") == 1
        assert wired.bus.pending_count("fixer", "#all") == 0
        assert wired.bus.pending_count("fixer", "PR111") == 1
        assert wired.bus.pending_count("fixer") == 1

    def test_inbox_order_follows_seq(self, wired):
        for i in range(5):
            wired.messaging.send("PR111", "fixer", f"m{i}")
        bodies = [m.body for m in wired.bus.inbox("fixer")]
        assert bodies == [f"m{i}" for i in range(5)]

    def test_pending_counts_groups_all_conversations_in_one_result(self, wired):
        wired.messaging.send("PR111", "fixer", "direct one")
        wired.messaging.send("PR111", "fixer", "direct two")
        wired.messaging.send("PR111", "#all", "global")

        assert wired.bus.pending_counts("fixer") == {"PR111": 2, "#all": 1}
        wired.messaging.acknowledge("fixer", "PR111")
        assert wired.bus.pending_counts("fixer") == {"#all": 1}

    def test_shared_history_page_contract(self, wired):
        for index in range(5):
            wired.messaging.send("PR111", "#all", f"m{index}")

        page = wired.views.channel_history_page("#all", limit=2)
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
        session_file.write_text("\n".join(json.dumps(record) for record in records))
        wired.threads.register(
            Thread(
                name="transcript-thread",
                tags=frozenset(),
                worktree=str(tmp_path),
                session_file=str(session_file),
            )
        )

        events = wired.transcripts.thread_transcript("transcript-thread")

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
        wired.threads.register(
            Thread(
                name="compacted-thread",
                tags=frozenset(),
                worktree=str(tmp_path),
                session_file=str(session_file),
            )
        )

        events = wired.transcripts.thread_transcript_page("compacted-thread").events

        assert len(events) == 1
        assert events[0].declared_name == "notice"
        assert "Important decisions" in events[0].text

    def test_claim_thread_can_baseline_inbox_atomically(self, wired):
        wired.messaging.send("PR111", "#all", "before claim")
        claimed = wired.threads.claim_thread(
            "viewer",
            tags=frozenset({"acp"}),
            worktree="/tmp/project",
            start_at_latest=True,
        )
        assert wired.bus.inbox(claimed.name) == []
        wired.messaging.send("PR111", "#all", "after claim")
        assert [message.body for message in wired.bus.inbox(claimed.name)] == ["after claim"]

    def test_runtime_info_is_exposed_in_presence(self, wired):
        wired.agents.set_agent_info(
            "fixer", model="openrouter/model", context_used=25, context_size=100
        )
        row = next(row for row in wired.views.who() if row["name"] == "fixer")
        assert row["model"] == "openrouter/model"
        assert row["context_percent"] == 25
        assert wired.agents.agent_info_of("fixer").context_used == 25

    def test_presence_omits_expensive_viewer_pending_counts(self, wired):
        rows = {row["name"]: row for row in wired.views.presence()}
        assert "pending" not in rows["fixer"]

    def test_runtime_info_rejects_unknown_thread(self, wired):
        with pytest.raises(UnregisteredThreadError):
            wired.agents.set_agent_info("ghost", model="model")

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

    def test_thread_detail(self, wired):
        detail = wired.views.thread_detail("fixer")
        assert detail["is_fork"] is True
        assert detail["task"] == "fix auth"
        assert detail["process_identity"] is None

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

        attached = wired.threads.attach_session("fixer", str(session_file), pid=123)

        assert attached.tags == frozenset({"auth"})
        assert attached.parent == "PR111"
        assert attached.task == "fix auth"
        assert attached.pid == 123
        assert attached.session_file == str(session_file.resolve())
        assert wired.registry.status("fixer").declared_name == "running"

    def test_release_only_allows_the_calling_thread(self, wired, monkeypatch):
        monkeypatch.setenv("PI_AGENT_ID", "fixer")

        with pytest.raises(RelationViolationError, match="cannot release"):
            wired.owners.release("PR111")

        wired.owners.release("fixer")
        assert wired.registry.status("fixer").declared_name == "stopped"

    def test_archive_requires_stopped_thread(self, wired):
        wired.messaging.send("PR111", "fixer", "kept after archive")
        with pytest.raises(RelationViolationError, match="Stop"):
            wired.threads.archive("fixer")
        wired.owners.stop("fixer")
        wired.threads.archive("fixer")
        assert wired.registry.status("fixer").declared_name == "archived"
        assert not any(row["name"] == "fixer" for row in wired.views.who())
        assert "fixer" not in wired.registry.active_threads()
        assert [message.body for message in wired.views.dm_history("PR111", "fixer")] == [
            "kept after archive"
        ]

    def test_rename_self_preserves_routing_history_and_state(self, wired, monkeypatch):
        wired.messaging.send("fixer", "PR111", "before rename")
        assert wired.messaging.acknowledge("PR111", "fixer") == 1
        wired.agents.set_activity("PR111", ActivityState.WORKING, "renaming")
        wired.agents.set_agent_info("PR111", model="test/model")
        wired.ledger.merge({"owner": "PR111", "members": ["PR111", "fixer"]}, author="PR111")
        monkeypatch.setenv("AGENT_COMMS_THREAD", "PR111")

        result = wired.threads.rename_self("planner")

        assert result.previous == "PR111"
        assert result.current == "planner"
        assert result.changed
        assert wired.registry.require("PR111").name == "planner"
        assert wired.registry.require("planner").tags == frozenset({"base"})
        assert wired.registry.require("fixer").parent == "planner"
        assert wired.agents.activity_of("planner").detail == "renaming"
        assert wired.agents.agent_info_of("planner").model == "test/model"
        assert wired.ledger.read()["owner"] == "planner"
        assert wired.bus.pending_count("planner", "fixer") == 0

        wired.messaging.send("fixer", "PR111", "old alias routes")
        wired.messaging.send("PR111", "fixer", "old process sends canonically")
        assert [message.body for message in wired.bus.inbox("planner", "fixer")] == [
            "old alias routes"
        ]
        history = wired.views.dm_history("PR111", "fixer")
        assert [message.body for message in history] == [
            "before rename",
            "old alias routes",
            "old process sends canonically",
        ]
        assert history[-2].target == "planner"
        assert history[-1].sender == "planner"
        with pytest.raises(RelationViolationError, match="cannot message itself"):
            wired.messaging.send("PR111", "planner", "alias self-DM")

    def test_rename_self_rejects_collisions_and_stopped_threads(self, wired, monkeypatch):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")
        with pytest.raises(RelationViolationError, match="already in use"):
            wired.threads.rename_self("PR111")
        wired.owners.stop("fixer")
        with pytest.raises(RelationViolationError, match="running"):
            wired.threads.rename_self("renamed")

    def test_rename_self_reclaims_own_alias_but_not_another_owners(self, wired, monkeypatch):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "PR111")
        assert wired.threads.rename_self("pr17").current == "pr17"
        assert wired.threads.rename_self("PR111").current == "PR111"
        assert wired.registry.snapshot().aliases == {"pr17": "PR111"}
        assert wired.registry.require("fixer").parent == "PR111"

        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")
        with pytest.raises(RelationViolationError, match="already in use"):
            wired.threads.rename_self("pr17")

    def test_managed_rename_normalizes_title_and_proves_owner(self, wired):
        wired.threads.register(
            Thread(
                name="generated-7",
                tags=frozenset({"acp"}),
                worktree="/tmp/project",
                process_identity=ProcessIdentity.capture(os.getpid()),
            )
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
        wired.threads.register(
            Thread(
                name="testing-123",
                tags=frozenset(),
                worktree="/tmp/other",
                process_identity=ProcessIdentity.capture(os.getppid()),
            )
        )
        wired.threads.register(
            Thread(
                name="generated-7",
                tags=frozenset({"acp"}),
                worktree="/tmp/project",
                process_identity=ProcessIdentity.capture(os.getpid()),
            )
        )

        result = wired.threads.rename_managed_thread("generated-7", "testing 123", owner_pid=os.getpid())

        assert result.current == "testing-123-2"

    def test_managed_rename_reclaims_own_alias(self, wired):
        wired.threads.register(
            Thread(name="generated-7", tags=frozenset(), worktree="/tmp/project", process_identity=ProcessIdentity.capture(os.getpid()))
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
        wired.threads.rename_self("reviewer")
        with pytest.raises(RelationViolationError, match="permanent alias"):
            wired.registry.register(Thread(name="fixer", tags=frozenset(), worktree="/tmp/other"))

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
        wired.threads.register(
            Thread(
                name="live-process",
                tags=frozenset(),
                worktree="/tmp",
                process_identity=ProcessIdentity.capture(process.pid),
            )
        )
        try:
            wired.owners.stop("live-process")
            assert process.wait(timeout=5) == -__import__("signal").SIGTERM
            assert wired.registry.status("live-process").declared_name == "stopped"
        finally:
            if process.poll() is None:
                process.kill()


class TestFork:
    def test_spec_requires_task(self):
        with pytest.raises(ValueError, match="task"):
            ForkSpec(name="x", parent="p", task="")

    def test_fork_requires_parent_session(self, wired):
        with pytest.raises(RelationViolationError, match="session file"):
            wired.threads.fork(ForkSpec(name="child", parent="PR111", task="t"))

    def test_fork_fail_closed_unknown_parent(self, wired):
        with pytest.raises(UnregisteredThreadError):
            wired.threads.fork(ForkSpec(name="child", parent="ghost", task="t"))


class TestLedgerOps:
    def test_recent_transcript_is_bounded_and_ordered(self, wired, tmp_path):
        import json

        from agent_comms.thread_management import _session_model

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
                        "message": {"role": "assistant", "content": "x" * 2_000_000},
                    }
                )
                + "\n"
            )
            for index in range(100):
                stream.write(
                    json.dumps(
                        {"type": "message", "message": {"role": "assistant", "content": str(index)}}
                    )
                    + "\n"
                )
        wired.threads.register(
            Thread(name="large", tags=frozenset(), worktree=str(tmp_path), session_file=str(path))
        )
        events = wired.transcripts.thread_transcript("large")
        assert events[0].declared_name == "notice"
        assert [event.text for event in events[1:]] == [str(index) for index in range(80, 100)]
        assert _session_model(path) == ("test", "one")

    def test_merge_requires_registered_author(self, wired):
        with pytest.raises(UnregisteredThreadError, match="Author"):
            wired.ledger.merge({"k": "v"}, author="ghost")

    def test_merge_records_author(self, wired):
        wired.ledger.merge({"k": "v"}, author="PR111")
        assert wired.ledger.read()["last_updated_by"] == "PR111"


class TestPollAndWire:
    def test_poll_snapshot(self, wired):
        wired.messaging.send("PR111", "fixer", "hello")
        snap = wired.views.poll("fixer")
        assert snap["thread"]["name"] == "fixer"
        assert len(snap["inbox"]) == 1
        assert [peer["name"] for peer in snap["peers"]] == ["PR111"]
        assert snap["peers"][0]["activity"] == "idle"
        assert "ledger" in snap

    def test_poll_current_thread_from_env(self, wired, monkeypatch, tmp_path):
        monkeypatch.setenv("PI_AGENT_ID", "fixer")
        monkeypatch.setenv("PI_WORKTREE", str(tmp_path))
        snap = wired.views.poll()
        assert snap["thread"]["name"] == "fixer"

    def test_adopt_current_registers_from_env(self, comms, monkeypatch, tmp_path):
        monkeypatch.setenv("PI_AGENT_ID", "me")
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
        comms.threads.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        assert (tmp_path / "fresh" / "registry.json").exists()


class TestCrossWireIsolation:
    def test_two_wires_do_not_leak(self, tmp_path):
        a = wire(tmp_path / "a")
        b = wire(tmp_path / "b")
        a.threads.register(Thread(name="only-in-a", tags=frozenset(), worktree="/wt"))
        assert "only-in-a" in a.registry
        with pytest.raises(UnregisteredThreadError):
            b.registry.require("only-in-a")


class TestReDeclarationPreservesProvenance:
    """Regression: a child that re-registers without tag env must keep
    the tags its fork declared, or it silently loses channel access."""

    def test_empty_tags_inherit_previous(self, wired):
        wired.threads.register(Thread(name="fixer", tags=frozenset(), worktree="/tmp/wt1"))
        assert wired.registry.require("fixer").tags == frozenset({"auth"})

    def test_explicit_tags_replace_previous(self, wired):
        wired.threads.register(Thread(name="fixer", tags=frozenset({"docs"}), worktree="/tmp/wt1"))
        assert wired.registry.require("fixer").tags == frozenset({"docs"})

    def test_missing_session_file_inherits_previous(self, wired, tmp_path):
        wired.threads.register(
            Thread(
                name="PR111",
                tags=frozenset({"base"}),
                worktree="/tmp/wt1",
                session_file=str(tmp_path / "s.json"),
            )
        )
        wired.threads.register(Thread(name="PR111", tags=frozenset({"base"}), worktree="/tmp/wt1"))
        assert wired.registry.require("PR111").session_file == str(tmp_path / "s.json")

    def test_fresh_declaration_is_unaffected(self, wired):
        wired.threads.register(Thread(name="fresh", tags=frozenset(), worktree="/wt"))
        assert wired.registry.require("fresh").tags == frozenset()

    def test_tagless_child_keeps_channel_after_reregister(self, wired):
        wired.threads.register(Thread(name="fixer", tags=frozenset(), worktree="/tmp/wt1"))
        wired.messaging.send("PR111", "#auth", "only tagged fixer sees this")
        assert wired.bus.pending_count("fixer") == 1

    def test_current_thread_reads_agent_comms_tags(self, monkeypatch, tmp_path):
        from agent_comms.threads import current_thread

        monkeypatch.delenv("PI_AGENT_ID", raising=False)
        monkeypatch.setenv("AGENT_COMMS_THREAD", "worker")
        monkeypatch.setenv("AGENT_COMMS_TAGS", "ci, docs")
        monkeypatch.chdir(tmp_path)
        assert current_thread().tags == frozenset({"ci", "docs"})
