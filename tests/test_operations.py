import json
import os
import signal
import subprocess
import sys

import pytest

from agent_comms import (
    ActivityState,
    ForkSpec,
    MessageType,
    RelationViolationError,
    Thread,
    UnregisteredThreadError,
    wire,
)
from agent_comms.bus_publication import PRIVATE_WIRE_FIELD
from agent_comms.declarations import _store_lock
from agent_comms.operations import _owner_launch_proof


class TestMessaging:
    def test_send_routes_through_bus(self, wired):
        mid = wired.send("PR111", "fixer", "hello", MessageType.QUESTION)
        assert isinstance(mid, str)
        assert wired.pending_count("fixer") == 1

    def test_send_fail_closed_unknown_sender(self, wired):
        with pytest.raises(UnregisteredThreadError, match="Sender"):
            wired.send("ghost", "fixer", "hello")

    def test_broadcast_reaches_all_peers(self, wired):
        wired.broadcast("PR111", "green")
        assert wired.pending_count("fixer") == 1

    def test_ack_clears_inbox(self, wired):
        wired.send("PR111", "fixer", "hello")
        assert wired.acknowledge("fixer") == 1
        assert wired.pending_count("fixer") == 0
        assert wired.acknowledge("fixer") == 0

    def test_scoped_ack_only_clears_selected_conversation(self, wired):
        wired.send("PR111", "fixer", "direct")
        wired.send("PR111", "#all", "global")

        assert wired.acknowledge("fixer", "PR111") == 1
        assert wired.pending_count("fixer", "PR111") == 0
        assert wired.pending_count("fixer", "#all") == 1
        assert wired.pending_count("fixer") == 1

    def test_scoped_channel_ack_does_not_clear_dm(self, wired):
        wired.send("PR111", "fixer", "direct")
        wired.send("PR111", "#all", "global")

        assert wired.acknowledge("fixer", "#all") == 1
        assert wired.pending_count("fixer", "#all") == 0
        assert wired.pending_count("fixer", "PR111") == 1
        assert wired.pending_count("fixer") == 1

    def test_inbox_order_follows_seq(self, wired):
        for i in range(5):
            wired.send("PR111", "fixer", f"m{i}")
        bodies = [m.body for m in wired.inbox("fixer")]
        assert bodies == [f"m{i}" for i in range(5)]

    def test_pending_counts_groups_all_conversations_in_one_result(self, wired):
        wired.send("PR111", "fixer", "direct one")
        wired.send("PR111", "fixer", "direct two")
        wired.send("PR111", "#all", "global")

        assert wired.pending_counts("fixer") == {"PR111": 2, "#all": 1}
        wired.acknowledge("fixer", "PR111")
        assert wired.pending_counts("fixer") == {"#all": 1}

    def test_shared_history_page_contract(self, wired):
        for index in range(5):
            wired.send("PR111", "#all", f"m{index}")

        page = wired.channel_history_page("#all", limit=2)
        assert [message.body for message in page.messages] == ["m3", "m4"]
        assert page.has_older
        assert wired.message_high_water() == 5


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
        wired.register(
            Thread(
                name="transcript-thread",
                tags=frozenset(),
                worktree=str(tmp_path),
                session_file=str(session_file),
            )
        )

        events = wired.thread_transcript("transcript-thread")

        assert [event.kind for event in events] == [
            "user",
            "thinking",
            "tool_start",
            "tool_end",
            "assistant",
        ]
        assert events[2].raw_input == {"to": "child"}
        assert events[3].text == "sent"
        row = next(row for row in wired.presence() if row["name"] == "transcript-thread")
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
        wired.register(
            Thread(
                name="compacted-thread",
                tags=frozenset(),
                worktree=str(tmp_path),
                session_file=str(session_file),
            )
        )

        events = wired.thread_transcript_page("compacted-thread").events

        assert len(events) == 1
        assert events[0].kind == "notice"
        assert "Important decisions" in events[0].text

    def test_claim_thread_can_baseline_inbox_atomically(self, wired):
        wired.send("PR111", "#all", "before claim")
        claimed = wired.claim_thread(
            "viewer",
            tags=frozenset({"acp"}),
            worktree="/tmp/project",
            start_at_latest=True,
        )
        assert wired.inbox(claimed.name) == []
        wired.send("PR111", "#all", "after claim")
        assert [message.body for message in wired.inbox(claimed.name)] == ["after claim"]

    def test_runtime_info_is_exposed_in_presence(self, wired):
        wired.set_agent_info("fixer", model="openrouter/model", context_used=25, context_size=100)
        row = next(row for row in wired.who() if row["name"] == "fixer")
        assert row["model"] == "openrouter/model"
        assert row["context_percent"] == 25
        assert wired.agent_info_of("fixer").context_used == 25

    def test_presence_omits_expensive_viewer_pending_counts(self, wired):
        rows = {row["name"]: row for row in wired.presence()}
        assert "pending" not in rows["fixer"]

    def test_runtime_info_rejects_unknown_thread(self, wired):
        with pytest.raises(UnregisteredThreadError):
            wired.set_agent_info("ghost", model="model")

    def test_list_threads_shape(self, wired):
        rows = {row["name"]: row for row in wired.list_threads()}
        assert set(rows) == {"PR111", "fixer"}
        assert rows["fixer"]["status"] == "running"
        assert rows["fixer"]["parent"] == "PR111"
        assert rows["fixer"]["pending"] == 0

    def test_list_active_only(self, wired):
        wired.stop("fixer")
        names = {row["name"] for row in wired.list_threads(active_only=True)}
        assert names == {"PR111"}

    def test_thread_detail(self, wired):
        detail = wired.thread_detail("fixer")
        assert detail["is_fork"] is True
        assert detail["task"] == "fix auth"
        assert detail["pid"] == 0

    def test_thread_detail_fail_closed(self, wired):
        with pytest.raises(UnregisteredThreadError):
            wired.thread_detail("ghost")

    def test_heartbeat_marks_running(self, wired):
        wired.stop("fixer")
        wired.heartbeat("fixer")
        assert wired.registry.status("fixer").value == "running"

    def test_attach_session_preserves_declaration_and_updates_runtime(self, wired, tmp_path):
        wired.stop("fixer")
        session_file = tmp_path / "session.jsonl"

        attached = wired.attach_session("fixer", str(session_file), pid=123)

        assert attached.tags == frozenset({"auth"})
        assert attached.parent == "PR111"
        assert attached.task == "fix auth"
        assert attached.pid == 123
        assert attached.session_file == str(session_file.resolve())
        assert wired.registry.status("fixer").value == "running"

    def test_release_only_allows_the_calling_thread(self, wired, monkeypatch):
        monkeypatch.setenv("PI_AGENT_ID", "fixer")

        with pytest.raises(RelationViolationError, match="cannot release"):
            wired.release("PR111")

        wired.release("fixer")
        assert wired.registry.status("fixer").value == "stopped"

    @pytest.mark.skipif(os.name != "posix", reason="inherited POSIX startup pipe")
    def test_reserved_worker_acquire_requires_launch_reservation(self, wired, monkeypatch):
        wired.register(Thread(name="reserved", tags=frozenset(), worktree="/tmp", pid=987654))
        before = wired.registry.snapshot().owner_epochs["reserved"]
        read_fd, write_fd = os.pipe()
        os.write(write_fd, _owner_launch_proof("reserved", 987654, before))
        os.close(write_fd)
        monkeypatch.setenv("AGENT_COMMS_RESERVATION_FD", str(read_fd))
        attached = wired.acquire_thread("reserved", owner_pid=987654)
        assert attached.pid == 987654
        assert wired.registry.snapshot().owner_epochs["reserved"] == before
        assert "AGENT_COMMS_RESERVATION_FD" not in os.environ
        # A new process that reuses a dead PID has no inherited launch pipe;
        # it must acquire a NEW epoch, never inherit the old reservation.
        wired.acquire_thread("reserved", owner_pid=987654)
        assert wired.registry.snapshot().owner_epochs["reserved"] > before

    @pytest.mark.skipif(os.name != "posix", reason="inherited POSIX startup pipe")
    def test_reserved_worker_rejects_stale_launch_epoch(self, wired, monkeypatch):
        wired.register(Thread(name="reserved", tags=frozenset(), worktree="/tmp", pid=987654))
        before = wired.registry.snapshot().owner_epochs["reserved"]
        read_fd, write_fd = os.pipe()
        os.write(write_fd, _owner_launch_proof("reserved", 987654, before))
        os.close(write_fd)
        wired.registry.register(wired.registry.require("reserved"))
        monkeypatch.setenv("AGENT_COMMS_RESERVATION_FD", str(read_fd))
        with pytest.raises(RelationViolationError, match="reservation no longer matches"):
            wired.acquire_thread("reserved", owner_pid=987654)
        assert wired.registry.snapshot().owner_epochs["reserved"] > before

    @pytest.mark.skipif(os.name != "posix", reason="inherited POSIX startup pipe")
    def test_long_valid_name_reservation_is_not_truncated(self, wired, monkeypatch):
        name = "n" * 257  # The old plaintext proof exceeded the 256-byte child read.
        wired.register(Thread(name=name, tags=frozenset(), worktree="/tmp", pid=987654))
        epoch = wired.registry.snapshot().owner_epochs[name]
        read_fd, write_fd = os.pipe()
        proof = _owner_launch_proof(name, 987654, epoch)
        assert len(proof) == 32
        os.write(write_fd, proof)
        os.close(write_fd)
        monkeypatch.setenv("AGENT_COMMS_RESERVATION_FD", str(read_fd))
        assert wired.acquire_thread(name, owner_pid=987654).pid == 987654
        assert wired.registry.snapshot().owner_epochs[name] == epoch

    @pytest.mark.skipif(os.name != "posix", reason="inherited POSIX startup pipe")
    def test_name_exceeding_pipe_capacity_has_fixed_size_launch_proof(self, wired, monkeypatch):
        name = "n" * 70000  # Larger than the usual Linux pipe capacity.
        inherited = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                self.pid = 987654
                assert kwargs["env"]["AGENT_COMMS_THREAD"] == name
                inherited.append(os.dup(kwargs["pass_fds"][0]))

        monkeypatch.setattr("agent_comms.operations.subprocess.Popen", FakePopen)
        try:
            with _store_lock(wired._wire_lock_path):
                owned = wired._launch_owner_unlocked(
                    Thread(name=name, tags=frozenset(), worktree="/tmp"), "/bin/echo"
                )
            epoch = wired.registry.snapshot().owner_epochs[name]
            assert owned.pid == 987654
            assert os.read(inherited[0], 33) == _owner_launch_proof(name, 987654, epoch)
        finally:
            for fd in inherited:
                os.close(fd)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group signaling")
    def test_stop_terminates_registered_process(self, wired, monkeypatch):
        signals = []
        wired.register(
            Thread(
                name="signal-test",
                tags=frozenset(),
                worktree="/tmp",
                pid=200,
            )
        )
        monkeypatch.setattr(
            "agent_comms.operations.Comms._is_local_participant",
            lambda *args, **kwargs: True,
        )
        monkeypatch.setattr(
            "agent_comms.operations.Comms._process_alive",
            lambda *args: not signals,
        )
        monkeypatch.setattr("agent_comms.operations.os.getpgid", lambda pid: pid)
        monkeypatch.setattr(
            "agent_comms.operations.os.killpg",
            lambda pid, sig: signals.append((pid, sig)),
        )

        wired.stop("signal-test")

        assert signals == [(200, __import__("signal").SIGTERM)]
        assert wired.registry.status("signal-test").value == "stopped"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner signaling")
    def test_stop_waits_for_owner_socket_without_holding_wire_lock(self, wired, monkeypatch):
        import threading

        from agent_comms.operations import _store_lock

        wired.register(Thread(name="starting", tags=frozenset(), worktree="/tmp", pid=987654))
        signals = []
        obtained = threading.Event()

        def prove_owner(self, thread, *, wait=True):
            if wait:
                # Use an ordinary worker lock acquisition, not a forged socket.
                def start_worker():
                    with _store_lock(wired._wire_lock_path):
                        obtained.set()

                worker = threading.Thread(target=start_worker, daemon=True)
                worker.start()
                assert obtained.wait(1), "stop held the wire lock through socket readiness"
                worker.join(1)
            return True

        monkeypatch.setattr("agent_comms.operations.Comms._is_local_participant", prove_owner)
        monkeypatch.setattr(
            "agent_comms.operations.Comms._process_alive", lambda *args: not signals
        )
        monkeypatch.setattr("agent_comms.operations.os.getpgid", lambda pid: pid)
        monkeypatch.setattr(
            "agent_comms.operations.os.killpg", lambda pid, sig: signals.append((pid, sig))
        )

        wired.stop("starting")
        assert signals == [(987654, __import__("signal").SIGTERM)]
        assert wired.registry.status("starting").value == "stopped"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner signaling")
    def test_stop_rejects_same_pid_new_epoch_before_signal(self, wired, monkeypatch):
        wired.register(Thread(name="starting", tags=frozenset(), worktree="/tmp", pid=987654))
        before = wired.registry.snapshot().owner_epochs["starting"]
        signals = []

        def epoch_changed(self, thread, *, wait=True):
            if wait:
                self.registry.register(thread)  # Same name/PID/created_at, NEW incarnation.
            return True

        monkeypatch.setattr("agent_comms.operations.Comms._process_alive", lambda *args: True)
        monkeypatch.setattr("agent_comms.operations.Comms._is_local_participant", epoch_changed)
        monkeypatch.setattr(
            "agent_comms.operations.Comms._signal_local_owner",
            staticmethod(lambda pid, sig: signals.append((pid, sig))),
        )
        with pytest.raises(RelationViolationError, match="epoch changed"):
            wired.stop("starting")
        assert signals == []
        assert wired.registry.snapshot().owner_epochs["starting"] > before
        assert wired.registry.status("starting").active

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner signaling")
    def test_stop_rejects_post_signal_same_pid_lifecycle_aba(self, wired, monkeypatch):
        wired.register(Thread(name="owner", tags=frozenset(), worktree="/tmp", pid=987654))
        monkeypatch.setattr("agent_comms.operations.Comms._process_alive", lambda *args: True)
        monkeypatch.setattr(
            "agent_comms.operations.Comms._is_local_participant", lambda *args, **kw: True
        )
        monkeypatch.setattr("agent_comms.operations.Comms._signal_local_owner", lambda *args: None)

        def later_incarnation(self, pid, seconds):
            self.registry.unregister("owner")
            self.registry.register(self.registry.require("owner"))
            self.registry.unregister("owner")
            return True

        monkeypatch.setattr("agent_comms.operations.Comms._wait_for_owner_exit", later_incarnation)
        with pytest.raises(RelationViolationError, match="Owner changed"):
            wired.stop("owner")
        assert wired.registry.status("owner") is not None

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner signaling")
    def test_stop_rejects_post_kill_same_pid_lifecycle_aba(self, wired, monkeypatch):
        wired.register(Thread(name="owner", tags=frozenset(), worktree="/tmp", pid=987654))
        before = wired.registry.snapshot().owner_epochs["owner"]
        original = wired.registry.require("owner")
        signals = []
        waits = []
        monkeypatch.setattr("agent_comms.operations.Comms._process_alive", lambda *args: True)
        monkeypatch.setattr(
            "agent_comms.operations.Comms._is_local_participant", lambda *args, **kw: True
        )
        monkeypatch.setattr(
            "agent_comms.operations.Comms._signal_local_owner",
            staticmethod(lambda pid, sig: signals.append((pid, sig))),
        )

        def exit_wait(self, pid, seconds):
            waits.append((pid, seconds))
            if len(waits) == 1:
                return False  # A has not exited after TERM; KILL is still authorized.
            with monkeypatch.context() as owner:
                owner.setenv("PI_AGENT_ID", "owner")
                owner.setattr("agent_comms.operations.os.getpid", lambda: 987654)
                self.release("owner")  # A's valid self-release after KILL.
                self.registry.register(self.registry.require("owner"))  # B reuses the PID.
                self.release("owner")  # B stops with identical declaration.
            return True

        monkeypatch.setattr("agent_comms.operations.Comms._wait_for_owner_exit", exit_wait)
        with pytest.raises(RelationViolationError, match="Owner changed"):
            wired.stop("owner")
        assert signals == [(987654, signal.SIGTERM), (987654, signal.SIGKILL)]
        assert len(waits) == 2
        assert wired.registry.require("owner") == original
        assert wired.registry.snapshot().owner_epochs["owner"] > before
        assert wired.registry.status("owner").value == "stopped"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner signaling")
    def test_stop_accepts_attested_release_after_unrelated_epoch_change(self, wired, monkeypatch):
        wired.register(Thread(name="owner", tags=frozenset(), worktree="/tmp", pid=987654))
        monkeypatch.setattr("agent_comms.operations.Comms._process_alive", lambda *args: True)
        monkeypatch.setattr(
            "agent_comms.operations.Comms._is_local_participant", lambda *args, **kw: True
        )
        monkeypatch.setattr("agent_comms.operations.Comms._signal_local_owner", lambda *args: None)

        def self_release(self, pid, seconds):
            # An unrelated owner changes the GLOBAL epoch before our worker
            # releases; E+1 arithmetic cannot attest its own release.
            self.registry.register(Thread(name="other", tags=frozenset(), worktree="/tmp"))
            with monkeypatch.context() as owner:
                owner.setenv("PI_AGENT_ID", "owner")
                owner.setattr("agent_comms.operations.os.getpid", lambda: 987654)
                self.release("owner")
            return True

        monkeypatch.setattr("agent_comms.operations.Comms._wait_for_owner_exit", self_release)
        wired.stop("owner")
        assert wired.registry.status("owner").value == "stopped"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner signaling")
    def test_stop_does_not_signal_replaced_pid_after_socket_wait(self, wired, monkeypatch):
        from dataclasses import replace

        wired.register(Thread(name="starting", tags=frozenset(), worktree="/tmp", pid=987654))
        signals = []

        def replaced_during_proof(self, thread, *, wait=True):
            if wait:
                self.registry.register(replace(thread, pid=987655))
            return True

        monkeypatch.setattr(
            "agent_comms.operations.Comms._is_local_participant", replaced_during_proof
        )
        monkeypatch.setattr("agent_comms.operations.Comms._process_alive", lambda *args: True)
        monkeypatch.setattr("agent_comms.operations.os.getpgid", lambda pid: pid)
        monkeypatch.setattr(
            "agent_comms.operations.os.killpg", lambda pid, sig: signals.append((pid, sig))
        )

        with pytest.raises(RelationViolationError, match="Owner changed"):
            wired.stop("starting")
        assert signals == []
        assert wired.registry.require("starting").pid == 987655

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner socket proof")
    def test_start_waits_outside_wire_lock_and_revalidates_owner(self, wired, monkeypatch):
        import threading

        from agent_comms.operations import _store_lock

        wired.register(Thread(name="starting", tags=frozenset(), worktree="/tmp", pid=987654))
        wired.registry.unregister("starting")
        obtained = threading.Event()

        def prove_owner(self, thread, *, wait=True):
            if wait:

                def startup():
                    with _store_lock(wired._wire_lock_path):
                        obtained.set()

                worker = threading.Thread(target=startup, daemon=True)
                worker.start()
                assert obtained.wait(1), "start held wire lock through socket readiness"
                worker.join(1)
            return True

        monkeypatch.setattr("agent_comms.operations.Comms._process_alive", lambda *args: True)
        monkeypatch.setattr("agent_comms.operations.Comms._is_local_participant", prove_owner)
        result = wired.start("starting")
        assert result.pid == 987654 and not result.launched
        assert wired.registry.status("starting").active

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner socket proof")
    def test_start_rejects_same_pid_new_epoch(self, wired, monkeypatch):
        wired.register(Thread(name="starting", tags=frozenset(), worktree="/tmp", pid=987654))
        wired.registry.unregister("starting")
        before = wired.registry.snapshot().owner_epochs["starting"]

        def epoch_changed(self, thread, *, wait=True):
            if wait:
                self.registry.register(thread)  # Same PID, later owner incarnation.
            return True

        monkeypatch.setattr("agent_comms.operations.Comms._process_alive", lambda *args: True)
        monkeypatch.setattr("agent_comms.operations.Comms._is_local_participant", epoch_changed)
        with pytest.raises(RelationViolationError, match="epoch changed"):
            wired.start("starting")
        assert wired.registry.snapshot().owner_epochs["starting"] > before
        assert wired.registry.require("starting").pid == 987654

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner socket proof")
    def test_start_refuses_replaced_pid_after_socket_wait(self, wired, monkeypatch):
        from dataclasses import replace

        wired.register(Thread(name="starting", tags=frozenset(), worktree="/tmp", pid=987654))
        wired.registry.unregister("starting")

        def replace_owner(self, thread, *, wait=True):
            if wait:
                self.registry.register(replace(thread, pid=987655))
            return True

        monkeypatch.setattr("agent_comms.operations.Comms._process_alive", lambda *args: True)
        monkeypatch.setattr("agent_comms.operations.Comms._is_local_participant", replace_owner)
        monkeypatch.setattr(
            "agent_comms.operations.Comms._launch_owner_unlocked",
            lambda *args, **kwargs: pytest.fail("replacement must not be launched"),
        )
        with pytest.raises(RelationViolationError, match="Owner changed"):
            wired.start("starting")
        assert wired.registry.require("starting").pid == 987655

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner signaling")
    def test_restart_preflights_whole_set_and_refuses_replaced_pid(self, wired, monkeypatch):
        from dataclasses import replace

        wired.register(Thread(name="restart-a", tags=frozenset(), worktree="/tmp", pid=987654))
        wired.register(Thread(name="restart-b", tags=frozenset(), worktree="/tmp", pid=987655))
        signaled = []

        def replace_owner(self, thread, *, wait=True):
            if wait and thread.name == "restart-a":
                self.registry.register(replace(thread, pid=987656))
            return True

        monkeypatch.setattr("agent_comms.operations.Comms._process_alive", lambda *args: True)
        monkeypatch.setattr("agent_comms.operations.Comms._is_local_participant", replace_owner)
        monkeypatch.setattr(
            "agent_comms.operations.Comms._signal_local_owner",
            lambda *args: signaled.append(args),
        )
        monkeypatch.setattr(
            "agent_comms.operations.Comms._launch_owner_unlocked",
            lambda *args, **kwargs: pytest.fail("replacement must not be launched"),
        )
        with pytest.raises(RelationViolationError, match="Owner selection changed"):
            wired.restart_owners(["restart-a", "restart-b"])
        assert signaled == []
        assert wired.registry.require("restart-a").pid == 987656
        assert wired.registry.require("restart-b").pid == 987655

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner signaling")
    def test_restart_rejects_same_pid_new_epoch_without_partial_signal(self, wired, monkeypatch):
        wired.register(Thread(name="restart-a", tags=frozenset(), worktree="/tmp", pid=987654))
        wired.register(Thread(name="restart-b", tags=frozenset(), worktree="/tmp", pid=987655))
        before = wired.registry.snapshot().owner_epochs["restart-a"]
        signals = []

        def epoch_changed(self, thread, *, wait=True):
            if wait and thread.name == "restart-a":
                self.registry.register(thread)
            return True

        monkeypatch.setattr("agent_comms.operations.Comms._process_alive", lambda *args: True)
        monkeypatch.setattr("agent_comms.operations.Comms._is_local_participant", epoch_changed)
        monkeypatch.setattr(
            "agent_comms.operations.Comms._signal_local_owner",
            staticmethod(lambda pid, sig: signals.append((pid, sig))),
        )
        with pytest.raises(RelationViolationError, match="epochs changed"):
            wired.restart_owners(["restart-a", "restart-b"])
        assert signals == []
        assert wired.registry.snapshot().owner_epochs["restart-a"] > before
        assert wired.registry.status("restart-b").active

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner signaling")
    def test_restart_rejects_post_signal_same_pid_lifecycle_aba(self, wired, monkeypatch):
        wired.register(Thread(name="owner", tags=frozenset(), worktree="/tmp", pid=987654))
        launches = []
        monkeypatch.setattr("agent_comms.operations.Comms._process_alive", lambda *args: True)
        monkeypatch.setattr(
            "agent_comms.operations.Comms._is_local_participant", lambda *args, **kw: True
        )
        monkeypatch.setattr("agent_comms.operations.Comms._signal_local_owner", lambda *args: None)

        def later_incarnation(self, pid, seconds):
            with monkeypatch.context() as owner:
                owner.setenv("PI_AGENT_ID", "owner")
                owner.setattr("agent_comms.operations.os.getpid", lambda: 987654)
                self.release("owner")  # A's valid receipt is superseded by B.
                self.registry.register(self.registry.require("owner"))
                self.release("owner")  # B reused PID and released normally.
            return True

        monkeypatch.setattr("agent_comms.operations.Comms._wait_for_owner_exit", later_incarnation)
        monkeypatch.setattr(
            "agent_comms.operations.Comms._launch_owner_unlocked",
            lambda *args, **kw: launches.append(args),
        )
        with pytest.raises(RelationViolationError, match="Owner changed"):
            wired.restart_owners(["owner"])
        assert launches == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner signaling")
    def test_restart_accepts_attested_self_release(self, wired, monkeypatch):
        wired.register(Thread(name="owner", tags=frozenset(), worktree="/tmp", pid=987654))
        launches = []
        monkeypatch.setattr("agent_comms.operations.Comms._process_alive", lambda *args: True)
        monkeypatch.setattr(
            "agent_comms.operations.Comms._is_local_participant", lambda *args, **kw: True
        )
        monkeypatch.setattr("agent_comms.operations.Comms._signal_local_owner", lambda *args: None)

        def self_release(self, pid, seconds):
            with monkeypatch.context() as owner:
                owner.setenv("PI_AGENT_ID", "owner")
                owner.setattr("agent_comms.operations.os.getpid", lambda: 987654)
                self.release("owner")
            return True

        def launch(self, thread, agent_bin, agent_args):
            launches.append(thread.name)
            return thread

        monkeypatch.setattr("agent_comms.operations.Comms._wait_for_owner_exit", self_release)
        monkeypatch.setattr("agent_comms.operations.Comms._launch_owner_unlocked", launch)
        result = wired.restart_owners(["owner"])
        assert launches == ["owner"]
        assert result[0].thread == "owner"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner signaling")
    def test_restart_releases_wire_lock_for_socket_and_graceful_exit(self, wired, monkeypatch):
        import threading
        from dataclasses import replace

        from agent_comms.operations import _store_lock

        wired.register(Thread(name="restart-a", tags=frozenset(), worktree="/tmp", pid=987654))
        signals = []

        def acquire_lock():
            complete = threading.Event()

            def worker():
                with _store_lock(wired._wire_lock_path):
                    complete.set()

            launched = threading.Thread(target=worker, daemon=True)
            launched.start()
            assert complete.wait(1), "restart held wire lock during worker startup/shutdown"
            launched.join(1)

        def prove_owner(self, thread, *, wait=True):
            if wait:
                acquire_lock()
            return True

        def await_exit(self, pid, seconds):
            assert signals == [(987654, __import__("signal").SIGTERM)]
            acquire_lock()
            return True

        def launch(self, thread, agent_bin, agent_args):
            owner = replace(thread, pid=987655)
            self.registry.register(owner)
            return owner

        monkeypatch.setattr("agent_comms.operations.Comms._process_alive", lambda *args: True)
        monkeypatch.setattr("agent_comms.operations.Comms._is_local_participant", prove_owner)
        monkeypatch.setattr("agent_comms.operations.Comms._wait_for_owner_exit", await_exit)
        monkeypatch.setattr(
            "agent_comms.operations.Comms._signal_local_owner",
            staticmethod(lambda pid, sig: signals.append((pid, sig))),
        )
        monkeypatch.setattr("agent_comms.operations.Comms._launch_owner_unlocked", launch)
        results = wired.restart_owners(["restart-a"])
        assert [(item.previous_pid, item.pid) for item in results] == [(987654, 987655)]
        assert wired.registry.require("restart-a").pid == 987655

    @pytest.mark.skipif(
        os.name != "posix" or not hasattr(os, "waitid") or not hasattr(os, "WNOWAIT"),
        reason="non-reaping waitid direct-child proof",
    )
    def test_stop_direct_child_exit_does_not_reap_popens_status(self, wired, monkeypatch):
        import signal

        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True
        )
        wired.register(Thread(name="child", tags=frozenset(), worktree="/tmp", pid=process.pid))
        try:
            monkeypatch.setattr(
                "agent_comms.operations.Comms._is_local_participant",
                lambda *args, **kwargs: True,
            )
            # Simulate an inconclusive ps result even after the child exits;
            # waitid supplies the direct parent's authoritative exit witness.
            monkeypatch.setattr("agent_comms.operations.Comms._process_alive", lambda *args: True)
            wired.stop("child")
            assert wired.registry.status("child").value == "stopped"
            assert process.wait(timeout=5) == -signal.SIGTERM
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_stop_marks_dead_process_stopped_without_signaling(self, wired, monkeypatch):
        wired.register(
            Thread(
                name="dead-process",
                tags=frozenset(),
                worktree="/tmp",
                pid=200,
            )
        )
        monkeypatch.setattr(
            "agent_comms.operations.Comms._process_alive",
            lambda *args: False,
        )
        monkeypatch.setattr(
            "agent_comms.operations.Comms._is_local_participant",
            lambda *args: pytest.fail("dead processes need no ownership check"),
        )

        wired.stop("dead-process")

        assert wired.registry.status("dead-process").value == "stopped"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX ps process lookup")
    def test_process_liveness_without_linux_proc(self, wired, monkeypatch):
        import os

        with monkeypatch.context() as patch:
            patch.setattr("agent_comms.operations.sys.platform", "darwin")
            patch.setenv("PATH", "")  # The E2E CLI uses this exact environment.
            assert wired._process_alive(os.getpid())
            assert not wired._process_alive(2**30)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX ps process lookup")
    def test_process_liveness_unknown_ps_result_never_marks_live_pid_dead(self, wired, monkeypatch):
        import os
        import subprocess

        with monkeypatch.context() as patch:
            patch.setattr("agent_comms.operations.sys.platform", "darwin")
            patch.setattr(
                "agent_comms.operations.subprocess.run",
                lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("ps")),
            )
            assert wired._process_alive(os.getpid())
            patch.setattr(
                "agent_comms.operations.subprocess.run",
                lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", ""),
            )
            assert wired._process_alive(os.getpid())
            patch.setattr(
                "agent_comms.operations.subprocess.run",
                lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "Z", ""),
            )
            assert not wired._process_alive(os.getpid())

    def test_process_liveness_checks_pid_before_spawning_ps(self, wired, monkeypatch):
        def missing_process(pid, signal):
            raise ProcessLookupError

        with monkeypatch.context() as patch:
            patch.setattr("agent_comms.operations.sys.platform", "darwin")
            patch.setattr("agent_comms.operations.os.kill", missing_process)
            patch.setattr(
                "agent_comms.operations.subprocess.run",
                lambda *args, **kwargs: pytest.fail("ps must not run for a missing PID"),
            )
            assert not wired._process_alive(123)

    def test_windows_process_liveness_uses_handles_not_kill(self, wired, monkeypatch):
        import ctypes
        from types import SimpleNamespace

        closed = []
        exit_code = [259]

        def open_process(*args):
            return 42

        def get_exit_code(handle, pointer):
            pointer._obj.value = exit_code[0]
            return 1

        def close_handle(handle):
            closed.append(handle)

        kernel = SimpleNamespace(
            OpenProcess=open_process, GetExitCodeProcess=get_exit_code, CloseHandle=close_handle
        )
        with monkeypatch.context() as patch:
            patch.setattr("agent_comms.operations.sys.platform", "win32")
            patch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
            assert wired._process_alive(123)
            exit_code[0] = 0
            assert not wired._process_alive(123)
        assert closed == [42, 42]

    def test_archive_requires_stopped_thread(self, wired):
        wired.send("PR111", "fixer", "kept after archive")
        with pytest.raises(RelationViolationError, match="Stop"):
            wired.archive("fixer")
        wired.stop("fixer")
        wired.archive("fixer")
        assert wired.registry.status("fixer").value == "archived"
        assert not any(row["name"] == "fixer" for row in wired.who())
        assert "fixer" not in wired.registry.active_threads()
        assert [message.body for message in wired.dm_history("PR111", "fixer")] == [
            "kept after archive"
        ]

    def test_rename_self_preserves_routing_history_and_state(self, wired, monkeypatch):
        wired.send("fixer", "PR111", "before rename")
        assert wired.acknowledge("PR111", "fixer") == 1
        wired.set_activity("PR111", ActivityState.WORKING, "renaming")
        wired.set_agent_info("PR111", model="test/model")
        wired.ledger_merge({"owner": "PR111", "members": ["PR111", "fixer"]}, author="PR111")
        monkeypatch.setenv("AGENT_COMMS_THREAD", "PR111")

        result = wired.rename_self("planner")

        assert result.previous == "PR111"
        assert result.current == "planner"
        assert result.changed
        assert wired.registry.require("PR111").name == "planner"
        assert wired.registry.require("planner").tags == frozenset({"base"})
        assert wired.registry.require("fixer").parent == "planner"
        assert wired.activity_of("planner").detail == "renaming"
        assert wired.agent_info_of("planner").model == "test/model"
        assert wired.ledger_read()["owner"] == "planner"
        assert wired.pending_count("planner", "fixer") == 0

        wired.send("fixer", "PR111", "old alias routes")
        wired.send("PR111", "fixer", "old process sends canonically")
        assert [message.body for message in wired.inbox("planner", "fixer")] == ["old alias routes"]
        history = wired.dm_history("PR111", "fixer")
        assert [message.body for message in history] == [
            "before rename",
            "old alias routes",
            "old process sends canonically",
        ]
        assert history[-2].target == "PR111"
        assert history[-1].sender == "planner"
        with pytest.raises(RelationViolationError, match="cannot message itself"):
            wired.send("PR111", "planner", "alias self-DM")

    def test_rename_self_rejects_collisions_and_stopped_threads(self, wired, monkeypatch):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")
        with pytest.raises(RelationViolationError, match="already in use"):
            wired.rename_self("PR111")
        wired.stop("fixer")
        with pytest.raises(RelationViolationError, match="running"):
            wired.rename_self("renamed")

    def test_managed_rename_normalizes_title_and_proves_owner(self, wired):
        wired.register(
            Thread(
                name="generated-7",
                tags=frozenset({"acp"}),
                worktree="/tmp/project",
                pid=os.getpid(),
            )
        )

        result = wired.rename_managed_thread("generated-7", "testing 123", owner_pid=os.getpid())

        assert result.previous == "generated-7"
        assert result.current == "testing-123"
        assert wired.registry.require("generated-7").name == "testing-123"
        with pytest.raises(RelationViolationError, match="does not own"):
            wired.rename_managed_thread("testing-123", "wrong", owner_pid=os.getpid() + 1)

    def test_managed_rename_disambiguates_duplicate_titles(self, wired):
        wired.register(
            Thread(
                name="testing-123",
                tags=frozenset(),
                worktree="/tmp/other",
                pid=123,
            )
        )
        wired.register(
            Thread(
                name="generated-7",
                tags=frozenset({"acp"}),
                worktree="/tmp/project",
                pid=456,
            )
        )

        result = wired.rename_managed_thread("generated-7", "testing 123", owner_pid=456)

        assert result.current == "testing-123-2"

    def test_old_alias_cannot_be_reused(self, wired, monkeypatch):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")
        wired.rename_self("reviewer")
        with pytest.raises(RelationViolationError, match="permanent alias"):
            wired.registry.register(Thread(name="fixer", tags=frozenset(), worktree="/tmp/other"))

    def test_delete_releases_renamed_identity(self, wired, monkeypatch):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")
        wired.rename_self("reviewer")
        wired.stop("reviewer")
        wired.delete("reviewer")
        wired.register(Thread(name="reviewer", tags=frozenset(), worktree="/tmp"))
        assert wired.registry.require("reviewer").name == "reviewer"
        assert "fixer" not in wired.registry

    def test_claim_reuses_alias_after_canonical_thread_is_deleted(self, wired, monkeypatch):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")
        wired.rename_self("reviewer")
        wired.stop("reviewer")
        wired.delete("reviewer")

        claimed = wired.claim_thread("fixer", tags=frozenset({"acp"}), worktree="/tmp/project")

        assert claimed.name == "fixer"

    def test_legacy_deleted_aliases_do_not_reserve_names(self, wired):
        import json

        path = wired.root / "registry.json"
        data = json.loads(path.read_text())
        data["aliases"] = {"deleted-old": "deleted-name", "deleted-name": "deleted-name"}
        path.write_text(json.dumps(data))
        claimed = wired.claim_thread("deleted-name", tags=frozenset(), worktree="/tmp")
        assert claimed.name == "deleted-name"
        assert "deleted-old" not in wired.registry

    def test_delete_requires_stopped_thread(self, wired):
        with pytest.raises(RelationViolationError, match="Stop"):
            wired.delete("fixer")

    def test_delete_detaches_children_without_changing_their_state(self, wired, tmp_path):
        from dataclasses import replace

        session = tmp_path / "child.jsonl"
        session.write_text("persisted child transcript\n")
        child = replace(wired.registry.require("fixer"), session_file=str(session))
        wired.register(child)
        wired.register(Thread(name="grandchild", tags=frozenset(), worktree="/tmp", parent="fixer"))
        wired.set_activity("fixer", ActivityState.THINKING, "still working")
        wired.send("fixer", "grandchild", "keep this exchange")
        last_seen = wired.registry.last_seen("fixer")
        wired.stop("PR111")
        result = wired.delete("PR111")
        assert result.detached_children == ("fixer",)
        assert "PR111" not in wired.registry
        assert wired.registry.require("fixer") == replace(child, parent=None)
        assert wired.registry.require("grandchild").parent == "fixer"
        assert wired.registry.status("fixer").value == "running"
        assert wired.registry.last_seen("fixer") == last_seen
        assert wired.activity_of("fixer").state is ActivityState.THINKING
        assert session.read_text() == "persisted child transcript\n"
        assert [m.body for m in wired.dm_history("fixer", "grandchild")] == ["keep this exchange"]

    @pytest.mark.parametrize("authority", ["unrelated_sideband", "protocol_marker", "torn_row"])
    def test_delete_rejects_private_or_uncertain_wire_before_begin_delete(
        self, wired, authority: str
    ) -> None:
        wired.send("PR111", "#all", "unrelated retained row")
        wired.send("fixer", "PR111", "subject to legacy purge")
        wired.stop("fixer")
        path = wired.bus._path
        sequence_path = path.parent / "bus_meta.json"
        aliases = wired.registry.aliases_for("fixer")
        if authority == "unrelated_sideband":
            records = path.read_bytes().splitlines(keepends=True)
            row = json.loads(records[0])
            row[PRIVATE_WIRE_FIELD] = {"publication_key": "SECRET"}
            records[0] = (json.dumps(row) + "\n").encode()
            path.write_bytes(b"".join(records))
        elif authority == "protocol_marker":
            metadata = json.loads(sequence_path.read_text())
            metadata["writer_protocol_version"] = 1
            sequence_path.write_text(json.dumps(metadata))
        else:
            with path.open("ab") as stream:
                stream.write(b'{"truncated":')
        before_bus, before_meta = path.read_bytes(), sequence_path.read_bytes()

        with pytest.raises(RelationViolationError, match="Private|Incomplete"):
            wired.delete("fixer")

        if authority == "protocol_marker":
            # A forged marker without a committed private registry guard
            # forbids even read projection, not just destructive deletion.
            with pytest.raises(RelationViolationError, match="Private registry guard"):
                wired.registry.status("fixer")
        else:
            assert wired.registry.status("fixer").value == "stopped"
            assert wired.registry.aliases_for("fixer") == aliases
        assert path.read_bytes() == before_bus
        assert sequence_path.read_bytes() == before_meta

    def test_delete_purges_owned_state_and_preserves_sequence(self, wired):
        wired.send("PR111", "fixer", "inbound dm")
        wired.send("fixer", "PR111", "outbound dm")
        wired.send("fixer", "#all", "authored channel")
        wired.send("PR111", "#all", "retained channel")
        wired.acknowledge("fixer", "PR111")
        wired.acknowledge("PR111", "fixer")
        wired.set_activity("fixer", ActivityState.WORKING, "delete me")
        wired.set_agent_info("fixer", model="test/model")
        wired.ledger_merge(
            {
                "fixer": {"state": "owned"},
                "owner": "fixer",
                "members": ["fixer", "PR111"],
            },
            author="fixer",
        )

        wired.stop("fixer")
        result = wired.delete("fixer")

        assert result.messages_removed == 3
        assert result.markers_removed == 2
        assert result.activity_events_removed == 1
        assert result.runtime_removed
        assert result.ledger_references_removed == 4
        assert "fixer" not in wired.registry
        assert [message.body for message in wired.full_history()] == ["retained channel"]
        assert "fixer" not in wired.all_activity()
        assert "fixer" not in wired.all_agent_info()
        assert wired.ledger_read() == {"members": ["PR111"]}
        assert all("fixer" not in key for key in wired.bus._read_markers())

        wired.send("PR111", "#all", "after delete")
        assert [message.seq for message in wired.full_history()] == [4, 5]

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
        wired.register(
            Thread(
                name="live-process",
                tags=frozenset(),
                worktree="/tmp",
                pid=process.pid,
            )
        )
        try:
            wired.stop("live-process")
            assert process.wait(timeout=5) == -__import__("signal").SIGTERM
            assert wired.registry.status("live-process").value == "stopped"
        finally:
            if process.poll() is None:
                process.kill()


class TestFork:
    def test_spec_requires_task(self):
        with pytest.raises(ValueError, match="task"):
            ForkSpec(name="x", parent="p", task="")

    def test_fork_requires_parent_session(self, wired):
        with pytest.raises(RelationViolationError, match="session file"):
            wired.fork(ForkSpec(name="child", parent="PR111", task="t"))

    def test_fork_fail_closed_unknown_parent(self, wired):
        with pytest.raises(UnregisteredThreadError):
            wired.fork(ForkSpec(name="child", parent="ghost", task="t"))

    def test_fork_launches_process_and_registers(self, wired, monkeypatch, tmp_path):
        session = tmp_path / "session.json"
        session.write_text("{}")

        parent = Thread(
            name="PR111",
            tags=frozenset({"base"}),
            worktree=str(tmp_path),
            pid=100,
            session_file=str(session),
        )
        wired.register(parent)

        launched: dict = {}

        class FakePopen:
            def __init__(self, args, env=None, cwd=None, **kwargs):
                launched["args"] = args
                launched["env"] = dict(env)
                launched["cwd"] = cwd
                launched["inherited_fd"] = (
                    os.dup(kwargs["pass_fds"][0]) if os.name == "posix" else -1
                )
                self.pid = 4242

        monkeypatch.setattr("agent_comms.operations.subprocess.Popen", FakePopen)
        monkeypatch.setenv("AGENT_COMMS_AGENT_BIN", "/opt/pi-coding-agent/bin/pi")
        try:
            child = wired.fork(ForkSpec(name="kid", parent="PR111", task="do it"))
        finally:
            if launched.get("inherited_fd", -1) >= 0:
                os.close(launched["inherited_fd"])

        assert child.pid == 4242
        assert launched["args"][1:] == ["-m", "agent_comms.worker"]
        assert launched["env"]["AGENT_COMMS_ROOT"] == str(wired.root.resolve())
        assert launched["env"]["AGENT_COMMS_AGENT_BIN"] == "/opt/pi-coding-agent/bin/pi"
        assert launched["env"]["PI_AGENT_ID"] == "kid"
        assert launched["env"]["PI_PARENT_ID"] == "PR111"
        assert launched["env"]["PI_TASK"] == "do it"
        assert launched["cwd"] == str(tmp_path)
        detail = wired.thread_detail("kid")
        assert detail["pid"] == 4242 and detail["parent"] == "PR111"

    def test_fork_uses_task_as_default_prompt(self, wired, monkeypatch, tmp_path):
        session = tmp_path / "session.json"
        session.write_text("{}")
        wired.register(
            Thread(
                name="PR111", tags=frozenset(), worktree=str(tmp_path), session_file=str(session)
            )
        )
        captured: dict = {}

        class FakePopen:
            def __init__(self, args, **kwargs):
                captured["args"] = args
                captured["env"] = kwargs["env"]
                captured["inherited_fd"] = (
                    os.dup(kwargs["pass_fds"][0]) if os.name == "posix" else -1
                )
                self.pid = 1

        monkeypatch.setattr("agent_comms.operations.subprocess.Popen", FakePopen)
        try:
            wired.fork(ForkSpec(name="kid", parent="PR111", task="the task"))
        finally:
            if captured.get("inherited_fd", -1) >= 0:
                os.close(captured["inherited_fd"])
        assert captured["env"]["PI_PROMPT"] == "the task"

    def test_fork_rolls_back_when_launch_raises(self, wired, monkeypatch, tmp_path):
        session = tmp_path / "session.json"
        session.write_text("{}")
        wired.register(
            Thread(
                name="PR111", tags=frozenset(), worktree=str(tmp_path), session_file=str(session)
            )
        )

        def boom(*args, **kwargs):
            raise OSError("no such binary")

        monkeypatch.setattr("agent_comms.operations.subprocess.Popen", boom)
        with pytest.raises(OSError):
            wired.fork(ForkSpec(name="kid", parent="PR111", task="t"))
        assert "kid" not in wired.registry


class TestLedgerOps:
    def test_recent_transcript_is_bounded_and_ordered(self, wired, tmp_path):
        import json

        from agent_comms.operations import _session_model

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
        wired.register(
            Thread(name="large", tags=frozenset(), worktree=str(tmp_path), session_file=str(path))
        )
        events = wired.thread_transcript("large")
        assert events[0].kind == "notice"
        assert [event.text for event in events[1:]] == [str(index) for index in range(80, 100)]
        assert _session_model(path) == ("test", "one")

    def test_merge_requires_registered_author(self, wired):
        with pytest.raises(UnregisteredThreadError, match="Author"):
            wired.ledger_merge({"k": "v"}, author="ghost")

    def test_merge_records_author(self, wired):
        wired.ledger_merge({"k": "v"}, author="PR111")
        assert wired.ledger_read()["last_updated_by"] == "PR111"


class TestPollAndWire:
    def test_poll_snapshot(self, wired):
        wired.send("PR111", "fixer", "hello")
        snap = wired.poll("fixer")
        assert snap["thread"]["name"] == "fixer"
        assert len(snap["inbox"]) == 1
        assert [peer["name"] for peer in snap["peers"]] == ["PR111"]
        assert snap["peers"][0]["activity"] == "idle"
        assert "ledger" in snap

    def test_poll_current_thread_from_env(self, wired, monkeypatch, tmp_path):
        monkeypatch.setenv("PI_AGENT_ID", "fixer")
        monkeypatch.setenv("PI_WORKTREE", str(tmp_path))
        snap = wired.poll()
        assert snap["thread"]["name"] == "fixer"

    def test_adopt_current_registers_from_env(self, comms, monkeypatch, tmp_path):
        monkeypatch.setenv("PI_AGENT_ID", "me")
        monkeypatch.setenv("PI_WORKTREE", str(tmp_path))
        thread = comms.adopt_current()
        assert thread.name == "me"
        assert comms.registry.status("me").value == "running"

    def test_wire_defaults_to_agent_comms_dir(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AGENT_COMMS_ROOT", raising=False)
        from agent_comms.operations import wire as wire_fn

        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / str(p).lstrip("~")))
        comms = wire_fn(None)
        assert comms.root == tmp_path / ".agent-comms"

    def test_wire_env_root(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_COMMS_ROOT", str(tmp_path / "custom"))
        comms = wire(None)
        assert comms.root == tmp_path / "custom"

    def test_runtime_files_created_on_demand(self, tmp_path):
        comms = wire(tmp_path / "fresh")
        comms.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        assert (tmp_path / "fresh" / "registry.json").exists()


class TestCrossWireIsolation:
    def test_two_wires_do_not_leak(self, tmp_path):
        a = wire(tmp_path / "a")
        b = wire(tmp_path / "b")
        a.register(Thread(name="only-in-a", tags=frozenset(), worktree="/wt"))
        assert "only-in-a" in a.registry
        with pytest.raises(UnregisteredThreadError):
            b.registry.require("only-in-a")


class TestReDeclarationPreservesProvenance:
    """Regression: a child that re-registers without tag env must keep
    the tags its fork declared, or it silently loses channel access."""

    def test_empty_tags_inherit_previous(self, wired):
        wired.register(Thread(name="fixer", tags=frozenset(), worktree="/tmp/wt1"))
        assert wired.registry.require("fixer").tags == frozenset({"auth"})

    def test_explicit_tags_replace_previous(self, wired):
        wired.register(Thread(name="fixer", tags=frozenset({"docs"}), worktree="/tmp/wt1"))
        assert wired.registry.require("fixer").tags == frozenset({"docs"})

    def test_missing_session_file_inherits_previous(self, wired, tmp_path):
        wired.register(
            Thread(
                name="PR111",
                tags=frozenset({"base"}),
                worktree="/tmp/wt1",
                session_file=str(tmp_path / "s.json"),
            )
        )
        wired.register(Thread(name="PR111", tags=frozenset({"base"}), worktree="/tmp/wt1"))
        assert wired.registry.require("PR111").session_file == str(tmp_path / "s.json")

    def test_fresh_declaration_is_unaffected(self, wired):
        wired.register(Thread(name="fresh", tags=frozenset(), worktree="/wt"))
        assert wired.registry.require("fresh").tags == frozenset()

    def test_tagless_child_keeps_channel_after_reregister(self, wired):
        wired.register(Thread(name="fixer", tags=frozenset(), worktree="/tmp/wt1"))
        wired.send("PR111", "#auth", "only tagged fixer sees this")
        assert wired.pending_count("fixer") == 1

    def test_fork_env_carries_neutral_tag_names(self, wired, monkeypatch, tmp_path):
        session = tmp_path / "session.json"
        session.write_text("{}")
        wired.register(
            Thread(
                name="PR111", tags=frozenset(), worktree=str(tmp_path), session_file=str(session)
            )
        )
        captured: dict = {}

        class FakePopen:
            def __init__(self, args, env=None, **kwargs):
                captured["env"] = dict(env)
                captured["inherited_fd"] = (
                    os.dup(kwargs["pass_fds"][0]) if os.name == "posix" else -1
                )
                self.pid = 1

        monkeypatch.setattr("agent_comms.operations.subprocess.Popen", FakePopen)
        try:
            wired.fork(ForkSpec(name="kid", parent="PR111", task="t", tags=frozenset({"ci"})))
        finally:
            if captured.get("inherited_fd", -1) >= 0:
                os.close(captured["inherited_fd"])
        assert captured["env"]["AGENT_COMMS_TAGS"] == "ci"
        assert captured["env"]["AGENT_COMMS_THREAD"] == "kid"
        assert captured["env"]["PI_AGENT_TAGS"] == "ci"

    def test_current_thread_reads_agent_comms_tags(self, monkeypatch, tmp_path):
        from agent_comms import current_thread

        monkeypatch.delenv("PI_AGENT_ID", raising=False)
        monkeypatch.setenv("AGENT_COMMS_THREAD", "worker")
        monkeypatch.setenv("AGENT_COMMS_TAGS", "ci, docs")
        monkeypatch.chdir(tmp_path)
        assert current_thread().tags == frozenset({"ci", "docs"})
