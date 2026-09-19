import pytest

from agent_comms import (
    ForkSpec,
    MessageType,
    RelationViolationError,
    Thread,
    UnregisteredThreadError,
    wire,
)


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

    def test_inbox_order_follows_seq(self, wired):
        for i in range(5):
            wired.send("PR111", "fixer", f"m{i}")
        bodies = [m.body for m in wired.inbox("fixer")]
        assert bodies == [f"m{i}" for i in range(5)]


class TestThreadOps:
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
        assert detail["pid"] == 200

    def test_thread_detail_fail_closed(self, wired):
        with pytest.raises(UnregisteredThreadError):
            wired.thread_detail("ghost")

    def test_heartbeat_marks_running(self, wired):
        wired.stop("fixer")
        wired.heartbeat("fixer")
        assert wired.registry.status("fixer").value == "running"


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
                self.pid = 4242

        monkeypatch.setattr("agent_comms.operations.subprocess.Popen", FakePopen)
        child = wired.fork(ForkSpec(name="kid", parent="PR111", task="do it"))

        assert child.pid == 4242
        assert launched["args"][1:3] == ["--fork", str(session)]
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
                self.pid = 1

        monkeypatch.setattr("agent_comms.operations.subprocess.Popen", FakePopen)
        wired.fork(ForkSpec(name="kid", parent="PR111", task="the task"))
        assert captured["args"][-2:] == ["-p", "the task"]

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
        assert snap["peers"] == ["PR111"]
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
