from pathlib import Path
from agent_comms.repo import Checkout
import json

import pytest

from agent_comms.channel_targets import CommandTarget
from agent_comms.channels import AnyChannel
from agent_comms.connector_caller import DirectorCaller
from agent_comms.errors import ToolRefusedError
from agent_comms.presentation import MessageNotification, SentMessageSummary
from agent_comms.threads import Thread
from agent_comms.tool_caller import NoThreadCaller, ThreadCaller
from agent_comms.tool_declaration import NativeRequestTools
from agent_comms.tools import invoke_tool


class TestToolCatalog:
    def test_native_launch_carries_the_existing_catalog_and_thread_tier(self, comms, tmp_path, monkeypatch):
        from pathlib import Path

        from agent_comms.cli_verbs.tools import ToolsCliCommand
        from agent_comms.native_arguments import NativeArguments
        from agent_comms.pi_vocabulary import ServiceTier
        from agent_comms.tool_caller import ThreadCaller

        monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(tmp_path / "config"))

        thread = Thread(
            name="catalog-owner", tags=frozenset(), worktree=Checkout(Path("/wt")), service_tier=ServiceTier.STANDARD,
        )
        comms.registry.declare(thread)
        flag, path = thread.native_launch_arguments(NativeArguments(()), tmp_path / "package", comms.root)
        assert flag == "--comms-catalog"
        payload = json.loads(Path(path).read_text())
        assert payload == ToolsCliCommand(name=ThreadCaller(thread.name)).apply(comms).to_wire()
        assert payload["service_tier"] == "standard"
        assert {tool["name"] for tool in payload["tools"]} == {
            tool["name"] for tool in NativeRequestTools.catalog()
        }

    def test_collaboration_tools_share_one_mutual_contact(self, comms, monkeypatch):
        monkeypatch.setenv("AGENT_COMMS_THREAD", "a")
        comms.registry.declare(Thread(name='a', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        comms.registry.declare(Thread(name='b', tags=frozenset(), worktree=Checkout(Path('/wt'))))
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
        assert not list(comms.views.history_page(AnyChannel.target()).messages)

    def test_collaboration_tool_rejects_bad_arguments_before_writes(self, comms, monkeypatch):
        monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
        with pytest.raises(ValueError):
            invoke_tool(comms, "comms_collaborations", {}, caller=NoThreadCaller())
        with pytest.raises(
            ValueError,
        ):
            invoke_tool(comms, "comms_collaboration", {"action": "start", "peer": "b"}, caller=NoThreadCaller())
        with pytest.raises(ValueError):
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

    def test_inbox_read_records_nothing(self, comms):
        comms.registry.declare(Thread(name='a', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        comms.registry.declare(Thread(name='b', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        comms.messaging.send("a", "b", "hello").message.message_id

        result = invoke_tool(comms, "comms_inbox", {"thread": "b"}, caller=NoThreadCaller())

        assert result["messages"][0]["text"] == "hello"
        assert comms.bus.pending_count("b") == 1

    def test_arguments_are_validated_before_dispatch(self, comms):
        with pytest.raises(
            ValueError,
        ):
            invoke_tool(comms, "comms_stop", {}, caller=NoThreadCaller())
        with pytest.raises(ValueError):
            invoke_tool(comms, "comms_stop", {"name": "a", "force": True}, caller=NoThreadCaller())
        with pytest.raises(ValueError):
            invoke_tool(comms, "comms_threads", {"active_only": "yes"}, caller=NoThreadCaller())

    def test_operator_threads_query_does_not_weaken_actions(self, comms, monkeypatch):
        from agent_comms.launch_runtime import CallingThreadUnavailable

        monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
        comms.registry.declare(Thread(name='listed', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        result = invoke_tool(comms, "comms_threads", {"active_only": True}, caller=NoThreadCaller())
        assert [thread["name"] for thread in result["threads"]] == ["listed"]
        assert result["sent_messages"] == []
        with pytest.raises(CallingThreadUnavailable):
            invoke_tool(comms, "comms_set_goal", {"text": "not authorized"}, caller=NoThreadCaller())

    def test_threads_tool_summarizes_each_recent_sent_message_once(self, comms, monkeypatch):
        # A tool consumes its bound caller, never a subsequently changed environment.
        monkeypatch.setenv("AGENT_COMMS_THREAD", "not-the-invoker")
        comms.registry.declare(Thread(name='sender', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        comms.registry.declare(Thread(name='recipient', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        sent = comms.messaging.send("sender", "recipient", "track me").message
        for index in range(6):
            comms.messaging.send("recipient", "sender", f"incoming {index}").message

        presentation = comms.views.thread_presentations(("sender",)).require("sender")
        assert all(row.message.reference != sent.reference for row in presentation.notifications)
        outcomes = presentation.notification_window.outcomes[sent.seq, sent.message_id]
        assert [summary.reference for summary in SentMessageSummary.recent(presentation)] == [sent.reference]

        result = invoke_tool(comms, "comms_threads", {}, caller=ThreadCaller("sender"))
        # One row per sent message, never one per recipient, and never the message body.
        assert result["sent_messages"] == [{
            "reference": {"seq": sent.seq, "message_id": str(sent.message_id)},
            "summary": MessageNotification.summary(outcomes),
        }]
        assert "track me" not in json.dumps(result["sent_messages"])

    def test_message_outcomes_reads_one_message_once_and_pages_its_recipients(self, comms):
        # The sender is a member of the channel; the frozen audience never lists its sender.
        comms.channels.create_tag("team")
        for name in ("sender", "alice", "bob", "carol"):
            comms.registry.declare(Thread(
                name=name, tags=frozenset({"team"}), worktree=Checkout(Path('/wt'))))
        sent = comms.messaging.send("sender", "#team", "count me once").message
        reference = {"seq": sent.seq, "message_id": str(sent.message_id)}

        first = invoke_tool(comms, "comms_message_outcomes", {"reference": reference, "limit": 2},
                            caller=ThreadCaller("sender"))
        assert first["message"]["seq"] == sent.seq
        assert json.dumps(first).count("count me once") == 1
        assert len(first["outcomes"]) == 2 and first["remaining"] == 1
        assert first["next"] == {"recipient": first["outcomes"][-1]["recipient_identity"]}
        rest = invoke_tool(comms, "comms_message_outcomes",
                           {"reference": reference, "limit": 2, "after": first["next"]},
                           caller=ThreadCaller("sender"))
        assert len(rest["outcomes"]) == 1 and rest["remaining"] == 0
        paged = [row["recipient_identity"]["canonical_thread"]
                 for row in (*first["outcomes"], *rest["outcomes"])]
        assert sorted(paged) == ["alice", "bob", "carol"]

    def test_message_outcomes_gives_a_sender_its_message_with_no_recipients(self, comms):
        comms.channels.create_tag("solo")
        comms.registry.declare(Thread(name='sender', tags=frozenset({"solo"}), worktree=Checkout(Path('/wt'))))
        sent = comms.messaging.send("sender", "#solo", "nobody else here").message
        read = invoke_tool(comms, "comms_message_outcomes",
                           {"reference": {"seq": sent.seq, "message_id": str(sent.message_id)}},
                           caller=ThreadCaller("sender"))
        assert read["message"]["seq"] == sent.seq
        assert read["outcomes"] == [] and read["remaining"] == 0

    def test_message_outcomes_shows_a_recipient_only_its_own_row_and_refuses_outsiders(self, comms):
        comms.channels.create_tag("team")
        for name in ("sender", "alice", "bob"):
            comms.registry.declare(Thread(
                name=name, tags=frozenset({"team"}), worktree=Checkout(Path('/wt'))))
        comms.registry.declare(Thread(name='outsider', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        sent = comms.messaging.send("sender", "#team", "who sees what").message
        reference = {"seq": sent.seq, "message_id": str(sent.message_id)}

        own = invoke_tool(comms, "comms_message_outcomes", {"reference": reference},
                          caller=ThreadCaller("alice"))
        assert [row["recipient_identity"]["canonical_thread"] for row in own["outcomes"]] == ["alice"]
        with pytest.raises(ToolRefusedError, match="is not readable by this caller"):
            invoke_tool(comms, "comms_message_outcomes", {"reference": reference},
                        caller=ThreadCaller("outsider"))
        # The director speaks for the owner, so it reads another sender's message whole.
        director = invoke_tool(comms, "comms_message_outcomes", {"reference": reference},
                               caller=DirectorCaller("outsider"))
        assert sorted(row["recipient_identity"]["canonical_thread"]
                      for row in director["outcomes"]) == ["alice", "bob"]

    def test_inbox_ack_can_target_one_conversation(self, comms):
        comms.registry.declare(Thread(name='a', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        comms.registry.declare(Thread(name='b', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        comms.registry.declare(Thread(name='c', tags=frozenset(), worktree=Checkout(Path('/wt'))))
        comms.messaging.send("a", "b", "from a").message.message_id
        comms.messaging.send("c", "b", "from c").message.message_id

        result = invoke_tool(
            comms, "comms_inbox", {"thread": "b", "target": "a"}, caller=NoThreadCaller()
        )

        assert [message["text"] for message in result["messages"]] == ["from a"]
        assert comms.bus.pending_count("b", CommandTarget.named("a")) == 1
        assert comms.bus.pending_count("b", CommandTarget.named("c")) == 1


