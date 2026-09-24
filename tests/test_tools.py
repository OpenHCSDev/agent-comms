import pytest

from agent_comms import Thread
from agent_comms.tools import (
    context_tool_catalog,
    invoke_context_tool,
    invoke_tool,
    tool_catalog,
)


class TestToolCatalog:
    def test_collaboration_tools_share_one_mutual_contact(self, comms, monkeypatch):
        monkeypatch.delenv("PI_AGENT_ID", raising=False)
        monkeypatch.setenv("AGENT_COMMS_THREAD", "a")
        comms.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        comms.register(Thread(name="b", tags=frozenset(), worktree="/wt"))
        before = comms.registry.snapshot()
        result = invoke_tool(comms, "comms_collaboration", {"action": "add", "peer": "b"})
        assert result["collaboration"]["owner"] == "a"
        assert result["collaboration"]["note"] == ""
        assert invoke_tool(comms, "comms_collaborations", {})["collaborations"] == [
            result["collaboration"]
        ]
        monkeypatch.setenv("AGENT_COMMS_THREAD", "b")
        mirrored = invoke_tool(comms, "comms_collaborations", {})["collaborations"]
        assert len(mirrored) == 1
        assert (mirrored[0]["owner"], mirrored[0]["peer"]) == ("b", "a")
        invoke_tool(
            comms, "comms_collaboration", {"action": "update", "peer": "a", "note": "Shared work"}
        )
        monkeypatch.setenv("AGENT_COMMS_THREAD", "a")
        collaborations = invoke_tool(comms, "comms_collaborations", {})["collaborations"]
        assert collaborations[0]["note"] == "Shared work"
        monkeypatch.setenv("AGENT_COMMS_THREAD", "b")
        invoke_tool(comms, "comms_collaboration", {"action": "remove", "peer": "a"})
        monkeypatch.setenv("AGENT_COMMS_THREAD", "a")
        assert invoke_tool(comms, "comms_collaborations", {}) == {"collaborations": []}
        monkeypatch.setenv("AGENT_COMMS_THREAD", "b")
        invoke_tool(comms, "comms_collaboration", {"action": "add", "peer": "a"})
        monkeypatch.setenv("AGENT_COMMS_THREAD", "a")
        assert invoke_tool(comms, "comms_collaborations", {})["collaborations"][0]["peer"] == "b"
        invoke_tool(comms, "comms_collaboration", {"action": "remove", "peer": "b"})
        assert comms.registry.snapshot() == before
        assert not comms.full_history()

    def test_collaboration_tool_rejects_bad_arguments_before_writes(self, comms, monkeypatch):
        monkeypatch.delenv("PI_AGENT_ID", raising=False)
        monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
        with pytest.raises(ValueError, match="identity"):
            invoke_tool(comms, "comms_collaborations", {})
        with pytest.raises(ValueError, match="must be one of"):
            invoke_tool(comms, "comms_collaboration", {"action": "start", "peer": "b"})
        with pytest.raises(ValueError, match="Unknown arguments"):
            invoke_tool(comms, "comms_collaboration", {"action": "add", "peer": "b", "owner": "c"})
        assert not (comms.root / "relationships.json").exists()

    def test_catalog_has_unique_json_schema_declarations(self):
        catalog = tool_catalog()
        names = [tool["name"] for tool in catalog]
        assert len(names) == len(set(names))
        assert {
            "comms_send",
            "comms_inbox",
            "comms_set_goal",
            "comms_stop",
            "comms_archive",
            "comms_delete",
        } <= set(names)
        assert all(tool["parameters"]["additionalProperties"] is False for tool in catalog)

    def test_thread_context_actions_are_declared_in_display_order(self):
        actions = context_tool_catalog("thread")
        assert [action["name"] for action in actions] == [
            "comms_fork",
            "comms_stop",
            "comms_start",
            "comms_archive",
            "comms_delete",
            "comms_ack",
        ]
        assert actions[0]["context_bindings"] == {"parent": "subject"}

    def test_inbox_ack_behavior_is_owned_by_declared_tool(self, comms):
        comms.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        comms.register(Thread(name="b", tags=frozenset(), worktree="/wt"))
        comms.send("a", "b", "hello")

        result = invoke_tool(comms, "comms_inbox", {"thread": "b"})

        assert result["messages"][0]["text"] == "hello"
        assert result["acknowledged"] == 1
        assert comms.pending_count("b") == 0

    def test_delete_tool_uses_shared_lifecycle_policy(self, comms):
        comms.register(Thread(name="running", tags=frozenset(), worktree="/wt"))
        with pytest.raises(ValueError, match="Stop a running thread"):
            invoke_tool(comms, "comms_delete", {"name": "running"})
        comms.stop("running")

        result = invoke_tool(comms, "comms_delete", {"name": "running"})

        assert result["deleted"] == "running"
        assert "running" not in comms.registry

    def test_arguments_are_validated_before_dispatch(self, comms):
        with pytest.raises(ValueError, match="Missing required argument"):
            invoke_tool(comms, "comms_stop", {})
        with pytest.raises(ValueError, match="Unknown arguments"):
            invoke_tool(comms, "comms_stop", {"name": "a", "force": True})
        with pytest.raises(ValueError, match="must be boolean"):
            invoke_tool(comms, "comms_threads", {"active_only": "yes"})

    def test_ack_can_target_one_conversation(self, comms):
        comms.register(Thread(name="a", tags=frozenset(), worktree="/wt"))
        comms.register(Thread(name="b", tags=frozenset(), worktree="/wt"))
        comms.register(Thread(name="c", tags=frozenset(), worktree="/wt"))
        comms.send("a", "b", "from a")
        comms.send("c", "b", "from c")

        result = invoke_context_tool(comms, "comms_ack", subject="a", actor="b")

        assert result == {"acknowledged": 1}
        assert comms.pending_count("b", "a") == 0
        assert comms.pending_count("b", "c") == 1
