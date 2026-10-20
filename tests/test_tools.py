import json
import re

import pytest

from agent_comms.channel_targets import CommandTarget
from agent_comms.channels import ANY_CHANNEL
from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller, NoThreadCaller
from agent_comms.tools import context_tool_catalog, invoke_context_tool, invoke_tool
from agent_comms.native_tools import NativeRequestTools


class TestToolCatalog:
    def test_native_launch_carries_the_existing_catalog_and_thread_tier(self, comms, tmp_path, monkeypatch):
        from pathlib import Path
        from agent_comms.pi_vocabulary import ServiceTier
        from agent_comms.native_arguments import NativeArguments
        from agent_comms.tools import ToolsCliCommand

        monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(tmp_path / "config"))

        thread = comms.registry.new_thread(
            name="catalog-owner", tags=frozenset(), worktree="/wt", service_tier=ServiceTier.STANDARD,
        )
        comms.registry.declare(thread)
        flag, path = thread.native_launch_arguments(NativeArguments(()), tmp_path / "package", comms.root)
        assert flag == "--comms-catalog"
        payload = json.loads(Path(path).read_text())
        assert payload == ToolsCliCommand(name=thread.name).apply(comms).to_wire()
        assert payload["service_tier"] == "standard"
        assert {tool["name"] for tool in payload["tools"]} == {
            tool["name"] for tool in NativeRequestTools.catalog()
        }

    def test_collaboration_tools_share_one_mutual_contact(self, comms, monkeypatch):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "a")
        comms.registry.declare(comms.registry.new_thread(name='a', tags=frozenset(), worktree='/wt'))
        comms.registry.declare(comms.registry.new_thread(name='b', tags=frozenset(), worktree='/wt'))
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
        assert not list(comms.views.history_page(ANY_CHANNEL).messages)

    def test_collaboration_tool_rejects_bad_arguments_before_writes(self, comms, monkeypatch):
        monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
        with pytest.raises(ValueError, match="thread caller"):
            invoke_tool(comms, "comms_collaborations", {}, caller=NoThreadCaller())
        with pytest.raises(
            ValueError,
            match=r"\A" + re.escape(
                "Invalid comms_collaboration arguments: Unknown RelationshipEdit name: 'start'"
            ) + r"\Z",
        ):
            invoke_tool(comms, "comms_collaboration", {"action": "start", "peer": "b"}, caller=NoThreadCaller())
        with pytest.raises(ValueError, match="Unknown fields"):
            invoke_tool(comms, "comms_collaboration", {"action": "add", "peer": "b", "owner": "c"}, caller=NoThreadCaller())
        assert not (comms.root / "relationships.json").exists()

    def test_catalog_exposes_adapter_json_schemas(self):
        catalog = NativeRequestTools.catalog()
        names = [tool["name"] for tool in catalog]
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
        comms.registry.declare(comms.registry.new_thread(name='a', tags=frozenset(), worktree='/wt'))
        comms.registry.declare(comms.registry.new_thread(name='b', tags=frozenset(), worktree='/wt'))
        comms.messaging.send("a", "b", "hello").message.message_id

        result = invoke_tool(comms, "comms_inbox", {"thread": "b"}, caller=NoThreadCaller())

        assert result["messages"][0]["text"] == "hello"
        assert result["acknowledged"] == 1
        assert comms.bus.pending_count("b") == 0

    def test_arguments_are_validated_before_dispatch(self, comms):
        with pytest.raises(
            ValueError,
            match=re.escape("Invalid comms_stop arguments: Missing required fields for CommsStopTool: name"),
        ):
            invoke_tool(comms, "comms_stop", {}, caller=NoThreadCaller())
        with pytest.raises(ValueError, match="Unknown fields"):
            invoke_tool(comms, "comms_stop", {"name": "a", "force": True}, caller=NoThreadCaller())
        with pytest.raises(ValueError, match="Expected.*bool"):
            invoke_tool(comms, "comms_threads", {"active_only": "yes"}, caller=NoThreadCaller())

    def test_operator_threads_query_does_not_weaken_actions(self, comms, monkeypatch):
        from agent_comms.launch_runtime import CallingThreadUnavailable

        monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
        comms.registry.declare(comms.registry.new_thread(name='listed', tags=frozenset(), worktree='/wt'))
        result = invoke_tool(comms, "comms_threads", {"active_only": True}, caller=NoThreadCaller())
        assert [thread["name"] for thread in result["threads"]] == ["listed"]
        assert result["sent_notifications"] == []
        with pytest.raises(CallingThreadUnavailable):
            invoke_tool(comms, "comms_set_goal", {"text": "not authorized"}, caller=NoThreadCaller())

    def test_threads_tool_includes_callers_bounded_sent_outcomes(self, comms, monkeypatch):
        # A tool consumes its bound caller, never a subsequently changed environment.
        monkeypatch.setenv("AGENT_COMMS_THREAD", "not-the-invoker")
        comms.registry.declare(comms.registry.new_thread(name='sender', tags=frozenset(), worktree='/wt'))
        comms.registry.declare(comms.registry.new_thread(name='recipient', tags=frozenset(), worktree='/wt'))
        sent = comms.messaging.send("sender", "recipient", "track me").message
        for index in range(6):
            comms.messaging.send("recipient", "sender", f"incoming {index}").message

        presentation = comms.views.thread_presentations(("sender",)).require("sender")
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
        ] == [(
            sent.seq, "track me", "recipient", {
                "kind": "contextual_progress",
                "details": [],
                "proof": {"kind": "unrecorded_progress", "details": []},
            },
        )]

    def test_inbox_ack_can_target_one_conversation(self, comms):
        comms.registry.declare(comms.registry.new_thread(name='a', tags=frozenset(), worktree='/wt'))
        comms.registry.declare(comms.registry.new_thread(name='b', tags=frozenset(), worktree='/wt'))
        comms.registry.declare(comms.registry.new_thread(name='c', tags=frozenset(), worktree='/wt'))
        comms.messaging.send("a", "b", "from a").message.message_id
        comms.messaging.send("c", "b", "from c").message.message_id

        result = invoke_tool(
            comms, "comms_inbox", {"thread": "b", "target": "a"}, caller=NoThreadCaller()
        )

        assert [message["text"] for message in result["messages"]] == ["from a"]
        assert result["acknowledged"] == 1
        assert comms.bus.pending_count("b", CommandTarget.named("a")) == 0
        assert comms.bus.pending_count("b", CommandTarget.named("c")) == 1


