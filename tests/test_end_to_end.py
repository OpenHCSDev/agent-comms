"""End-to-end test: fork -> send -> receive -> respond, through the real CLI.

Simulates a full coordination lifecycle using actual subprocesses (the pi
binary is faked, but every other hop goes through the real CLI protocol):
two CLI-registered threads exchange messages, a child forks, the child reads
its inbox and responds, and the parent acknowledges.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
SRC = REPO / "src"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="E2E fork uses a shell-script pi stub; POSIX only "
    "(fork mechanics covered by unit tests)",
)


def cli(root: Path, *argv: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "agent_comms.cli", "--root", str(root), *argv],
        capture_output=True,
        text=True,
        env={"PATH": "", "PYTHONPATH": str(SRC)},
    )
    assert result.returncode == 0, f"CLI failed: {result.stdout}{result.stderr}"
    return json.loads(result.stdout)


def run_python(
    code: str, thread: str, parent: str, root: Path, extra_env: dict | None = None
) -> dict:
    """Simulate a running agent process: env-plumbed thread identity."""
    env = {
        "PATH": "",
        "PYTHONPATH": str(SRC),
        "PI_AGENT_ID": thread,
        "PI_PARENT_ID": parent,
        "PI_WORKTREE": str(root),
        "AGENT_COMMS_ROOT": str(root),
    }
    env.update(extra_env or {})
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"agent process failed: {result.stderr}"
    return json.loads(result.stdout)


class TestEndToEndLifecycle:
    def test_fork_send_receive_respond(self, tmp_path):
        root = tmp_path / "wire"

        # 1. Parent thread starts and registers itself (pi records its session).
        stub = tmp_path / "fake-pi"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        session = tmp_path / "session.json"
        session.write_text("{}")
        run_python(
            "import json; from agent_comms import current_thread, wire;"
            " c = wire(); c.register(c.adopt_current());"
            " print(json.dumps({'name': c.registry.require('PR111').name}))",
            thread="PR111",
            parent="",
            root=root,
            extra_env={"PI_SESSION_FILE": str(session)},
        )
        # A real pi process back-fills its session file into the registry;
        # emulate that here so the parent is forkable.
        from agent_comms import Thread
        from agent_comms.operations import wire as wire_root

        comms = wire_root(root)
        parent = comms.registry.require("PR111")
        comms.register(
            Thread(
                name=parent.name,
                tags=parent.tags,
                worktree=parent.worktree,
                parent=parent.parent,
                task=parent.task,
                pid=parent.pid,
                session_file=str(session),
            )
        )

        # 2. Parent forks a child (pi binary faked with a stub script).
        out = cli(
            root,
            "fork",
            "--name",
            "kid",
            "--parent",
            "PR111",
            "--task",
            "review the diff",
            "--pi-bin",
            str(stub),
        )
        assert out["forked"] == "kid"
        # The child was launched in the parent's worktree and registered.
        detail = cli(root, "thread", "--name", "kid")
        assert detail["parent"] == "PR111" and detail["task"] == "review the diff"
        assert detail["pid"] > 0

        # 3. Child registers itself the way a real pi process would.
        run_python(
            "import json; from agent_comms import current_thread, wire;"
            " c = wire(); c.register(c.adopt_current());"
            " print(json.dumps({'ok': 'kid' in str(c.registry.all_threads())}))",
            thread="kid",
            parent="PR111",
            root=root,
        )

        # 4. Parent sends a handoff to the child.
        cli(
            root,
            "send",
            "--from",
            "PR111",
            "--to",
            "kid",
            "--body",
            "please review PR 112",
            "--type",
            "handoff",
        )

        # 5. Child polls, reads the inbox, and responds through the CLI.
        snap = run_python(
            "import json; from agent_comms import wire;"
            " c = wire(); print(json.dumps(c.poll('kid')))",
            thread="kid",
            parent="PR111",
            root=root,
        )
        assert snap["thread"]["name"] == "kid"
        assert [m["text"] for m in snap["inbox"]] == ["please review PR 112"]
        assert snap["inbox"][0]["type"] == "handoff"

        cli(
            root,
            "send",
            "--from",
            "kid",
            "--to",
            "PR111",
            "--body",
            "review complete, one nit",
            "--type",
            "ack",
        )

        # 6. Parent reads the response and acknowledges.
        acked = cli(root, "ack", "--thread", "PR111")
        assert acked == {"acknowledged": 1}
        parent_inbox = cli(root, "inbox", "--thread", "PR111")
        assert [m["text"] for m in parent_inbox["messages"]] == []

    def test_broadcast_reaches_every_thread_via_cli(self, tmp_path):
        root = tmp_path / "wire"
        for name in ("a", "b", "c"):
            cli(root, "register", "--name", name, "--worktree", str(tmp_path))
        cli(root, "send", "--from", "a", "--to", "broadcast", "--body", "heads up")
        for name in ("b", "c"):
            inbox = cli(root, "inbox", "--thread", name)
            assert [m["text"] for m in inbox["messages"]] == ["heads up"]
        # Sender does not receive its own broadcast.
        assert cli(root, "inbox", "--thread", "a")["messages"] == []

    def test_acp_client_and_cli_share_one_wire(self, tmp_path):
        """ACP sessions and CLI agents see the same threads and messages.

        The real stdio roundtrip lives in tests/test_acp.py; here we prove
        the CLI side of the same wire.
        """
        import asyncio

        from agent_comms.acp import CommsAgent
        from agent_comms.operations import wire

        root = tmp_path / "wire"
        comms = wire(root)
        agent = CommsAgent(comms, reply_window=0.2, no_reply_window=0.1, reply_quiet=0.05)

        async def flow() -> None:
            await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])

        asyncio.run(flow())

        # CLI thread messages the ACP-registered thread.
        cli(root, "register", "--name", "cli-agent", "--worktree", str(tmp_path))
        cli(root, "send", "--from", "cli-agent", "--to", "proj", "--body", "from the cli side")

        # ACP prompt drains it as agent message chunks.
        sent: list = []

        class FakeClient:
            async def session_update(self, session_id=None, update=None, **kw):
                sent.append(update)

        agent._client = FakeClient()

        async def prompt_flow() -> None:
            await agent.prompt(
                session_id="proj", prompt=[{"type": "text", "text": "!relay checking inbox"}]
            )

        asyncio.run(prompt_flow())
        assert len(sent) == 1
        assert "from the cli side" in sent[0].content.text
        # The ACP prompt itself was broadcast and is visible on the CLI side.
        inbox = cli(root, "inbox", "--thread", "cli-agent")
        assert [m["text"] for m in inbox["messages"]] == ["checking inbox"]
        # And the CLI side can reply, visible in the thread's DM history.
        cli(root, "send", "--from", "cli-agent", "--to", "proj", "--body", "roger")
        dm = [m.body for m in wire(root).dm_history("cli-agent", "proj")]
        assert dm == ["from the cli side", "roger"]
