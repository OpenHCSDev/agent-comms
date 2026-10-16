import re

import pytest

from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller, NoThreadCaller, ToolCaller
from agent_comms.tools import context_tool_catalog, invoke_context_tool, invoke_tool, tool_catalog


def test_tool_entry_decodes_caller_states_once():
    assert ToolCaller.from_environment({}) == NoThreadCaller()
    assert ToolCaller.from_environment({"AGENT_COMMS_THREAD": ""}) == NoThreadCaller()
    assert ToolCaller.from_environment({"AGENT_COMMS_THREAD": "owner"}) == ThreadCaller("owner")
    assert ToolCaller.from_actor(None) == NoThreadCaller()
    assert ToolCaller.from_actor("owner") == ThreadCaller("owner")


class TestToolCatalog:
    def test_collaboration_tools_share_one_mutual_contact(self, comms, monkeypatch):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "a")
        comms.registry.declare(Thread(name="a", tags=frozenset(), worktree="/wt"))
        comms.registry.declare(Thread(name="b", tags=frozenset(), worktree="/wt"))
        before = comms.registry.snapshot()
        result = invoke_tool(comms, "comms_collaboration", {"action": "add", "peer": "b"}, caller=ThreadCaller("a"))
        assert result["collaboration"]["owner"] == "a"
        assert result["collaboration"]["note"] == ""
        assert invoke_tool(comms, "comms_collaborations", {}, caller=ThreadCaller("a"))["collaborations"] == [
            result["collaboration"]
        ]
        monkeypatch.setenv("AGENT_COMMS_THREAD", "b")
        mirrored = invoke_tool(comms, "comms_collaborations", {}, caller=ThreadCaller("b"))["collaborations"]
        assert len(mirrored) == 1
        assert (mirrored[0]["owner"], mirrored[0]["peer"]) == ("b", "a")
        invoke_tool(
            comms, "comms_collaboration", {"action": "update", "peer": "a", "note": "Shared work"}, caller=ThreadCaller("b")
        )
        monkeypatch.setenv("AGENT_COMMS_THREAD", "a")
        collaborations = invoke_tool(comms, "comms_collaborations", {}, caller=ThreadCaller("a"))["collaborations"]
        assert collaborations[0]["note"] == "Shared work"
        monkeypatch.setenv("AGENT_COMMS_THREAD", "b")
        invoke_tool(comms, "comms_collaboration", {"action": "remove", "peer": "a"}, caller=ThreadCaller("b"))
        monkeypatch.setenv("AGENT_COMMS_THREAD", "a")
        assert invoke_tool(comms, "comms_collaborations", {}, caller=ThreadCaller("a")) == {"collaborations": []}
        monkeypatch.setenv("AGENT_COMMS_THREAD", "b")
        invoke_tool(comms, "comms_collaboration", {"action": "add", "peer": "a"}, caller=ThreadCaller("b"))
        monkeypatch.setenv("AGENT_COMMS_THREAD", "a")
        assert invoke_tool(comms, "comms_collaborations", {}, caller=ThreadCaller("a"))["collaborations"][0]["peer"] == "b"
        invoke_tool(comms, "comms_collaboration", {"action": "remove", "peer": "b"}, caller=ThreadCaller("a"))
        assert comms.registry.snapshot() == before
        assert not comms.views.full_history()

    def test_collaboration_tool_rejects_bad_arguments_before_writes(self, comms, monkeypatch):
        monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
        with pytest.raises(ValueError, match="thread caller"):
            invoke_tool(comms, "comms_collaborations", {})
        with pytest.raises(
            ValueError,
            match=re.escape(
                "Invalid comms_collaboration arguments: Unknown RelationshipEdit name: 'start'"
            ),
        ):
            invoke_tool(comms, "comms_collaboration", {"action": "start", "peer": "b"})
        with pytest.raises(ValueError, match="Unknown fields"):
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
        } <= set(names)
        assert all(tool["parameters"]["additionalProperties"] is False for tool in catalog)

    @pytest.mark.refactor_guard
    def test_thread_context_actions_are_declared_in_display_order(self):
        actions = context_tool_catalog("thread")
        assert [action["name"] for action in actions] == [
            "comms_fork",
            "comms_stop",
            "comms_start",
            "comms_queue_restart",
            "comms_archive",
        ]
        assert actions[0]["context_bindings"] == {"parent": "subject"}

    def test_inbox_ack_behavior_is_owned_by_declared_tool(self, comms):
        comms.registry.declare(Thread(name="a", tags=frozenset(), worktree="/wt"))
        comms.registry.declare(Thread(name="b", tags=frozenset(), worktree="/wt"))
        comms.messaging.send("a", "b", "hello")

        result = invoke_tool(comms, "comms_inbox", {"thread": "b"})

        assert result["messages"][0]["text"] == "hello"
        assert result["acknowledged"] == 1
        assert comms.bus.pending_count("b") == 0

    def test_arguments_are_validated_before_dispatch(self, comms):
        with pytest.raises(
            ValueError,
            match=re.escape("Invalid comms_stop arguments: Missing required fields for CommsStopTool: name"),
        ):
            invoke_tool(comms, "comms_stop", {})
        with pytest.raises(ValueError, match="Unknown fields"):
            invoke_tool(comms, "comms_stop", {"name": "a", "force": True})
        with pytest.raises(ValueError, match="Expected.*bool"):
            invoke_tool(comms, "comms_threads", {"active_only": "yes"})

    def test_threads_query_without_caller_does_not_weaken_actions(self, comms, monkeypatch):
        from agent_comms.launch_runtime import CallingThreadUnavailable

        monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
        comms.registry.declare(Thread(name="listed", tags=frozenset(), worktree="/wt"))
        result = invoke_tool(comms, "comms_threads", {"active_only": True})
        assert [thread["name"] for thread in result["threads"]] == ["listed"]
        assert result["sent_notifications"] == []
        with pytest.raises(CallingThreadUnavailable):
            invoke_tool(comms, "comms_set_goal", {"text": "not authorized"})

    def test_threads_tool_includes_callers_bounded_sent_outcomes(self, comms, monkeypatch):
        # A tool consumes its bound caller, never a subsequently changed environment.
        monkeypatch.setenv("AGENT_COMMS_THREAD", "not-the-invoker")
        comms.registry.declare(Thread(name="sender", tags=frozenset(), worktree="/wt"))
        comms.registry.declare(Thread(name="recipient", tags=frozenset(), worktree="/wt"))
        sent = comms.messaging.send_message("sender", "recipient", "track me")
        for index in range(6):
            comms.messaging.send_message("recipient", "sender", f"incoming {index}")

        presentation = comms.views.thread_presentation("sender")
        assert all(row.message.reference != sent.reference for row in presentation.notifications)
        assert [row.message.reference for row in presentation.sent_notifications] == [sent.reference]

        result = invoke_tool(comms, "comms_threads", {}, caller=ThreadCaller("sender"))
        assert [
            (
                row["message"]["seq"],
                row["message"]["text"],
                row["recipient_identity"]["canonical_thread"],
                row["state"],
            )
            for row in result["sent_notifications"]
        ] == [(sent.seq, "track me", "recipient", "Sent")]

    def test_inbox_ack_can_target_one_conversation(self, comms):
        comms.registry.declare(Thread(name="a", tags=frozenset(), worktree="/wt"))
        comms.registry.declare(Thread(name="b", tags=frozenset(), worktree="/wt"))
        comms.registry.declare(Thread(name="c", tags=frozenset(), worktree="/wt"))
        comms.messaging.send("a", "b", "from a")
        comms.messaging.send("c", "b", "from c")

        result = invoke_tool(
            comms, "comms_inbox", {"thread": "b", "target": "a"}
        )

        assert [message["text"] for message in result["messages"]] == ["from a"]
        assert result["acknowledged"] == 1
        assert comms.bus.pending_count("b", "a") == 0
        assert comms.bus.pending_count("b", "c") == 1


@pytest.mark.refactor_guard
def test_new_tool_owner_derives_catalog_schema_binding_and_real_invocation(comms, monkeypatch):
    from dataclasses import dataclass

    from agent_comms.tools import SubjectBinding, ToolRequest, tool_field

    from agent_comms.tool_declaration import ToolDeclaration

    monkeypatch.setattr(ToolDeclaration, "__registry__", dict(ToolDeclaration.__registry__))

    @dataclass(frozen=True, kw_only=True)
    class InspectInboxTool(ToolRequest):
        label = "Inspect an inbox"
        description = "Test-only declaration extends the complete adapter path."
        context = "thread"
        target: str = tool_field("Selected peer", binding=SubjectBinding)
        viewer: str = tool_field("Viewing thread")
        include_body: bool = tool_field("Include message bodies", default=False)

        def apply(self, comms, caller: ToolCaller):
            messages = comms.bus.inbox(self.viewer, self.target)
            return {
                "count": len(messages),
                "bodies": [row.body for row in messages] if self.include_body else [],
            }

    for name in ("a", "b"):
        comms.registry.declare(Thread(name=name, tags=frozenset(), worktree="/wt"))
    comms.messaging.send("a", "b", "Actual inbox source")
    schema = next(row for row in tool_catalog() if row["name"] == InspectInboxTool.declared_name)
    assert schema["parameters"]["required"] == ["target", "viewer"]
    assert schema["parameters"]["properties"]["include_body"]["default"] is False
    assert schema["context_bindings"] == {"target": "subject"}
    assert schema in context_tool_catalog("thread")
    result = invoke_context_tool(
        comms,
        InspectInboxTool.declared_name,
        subject="a",
        actor="b",
        arguments={"viewer": "b", "include_body": True},
    )
    assert result == {"count": 1, "bodies": ["Actual inbox source"]}
    for arguments in (
        {"viewer": "b", "include_body": "yes"},
        {"viewer": "b", "include_body": None},
        {"viewer": "b", "tool": "forged"},
    ):
        with pytest.raises(ValueError):
            invoke_context_tool(
                comms, InspectInboxTool.declared_name, subject="a", actor="b", arguments=arguments
            )
