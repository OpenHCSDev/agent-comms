import json
import subprocess
import sys
from pathlib import Path

from agent_comms.acp import AcpServer
from agent_comms.operations import wire


class StringIO:
    """Minimal line-iterable reader / writable writer."""

    def __init__(self, lines: list[str]):
        self._lines = lines
        self.written: list[str] = []

    def __iter__(self):
        return iter(self._lines)

    def write(self, text: str) -> None:
        self.written.append(text)

    def flush(self) -> None:
        pass


def make_server(root: Path, requests: list[dict]) -> tuple[AcpServer, StringIO]:
    comms = wire(root)
    lines = [json.dumps(r) for r in requests]
    out = StringIO([])
    server = AcpServer(comms, reader=lines, writer=out)
    return server, out


def parse_written(out: StringIO) -> list[dict]:
    return [json.loads(line) for line in out.written]


class TestInitialize:
    def test_returns_protocol_version_and_capabilities(self, tmp_path):
        server, _ = make_server(tmp_path, [])
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert response["result"]["protocolVersion"] == 1
        assert "agentCapabilities" in response["result"]


class TestSessionNew:
    def test_registers_thread_and_returns_session_id(self, tmp_path):
        comms = wire(tmp_path)
        server = AcpServer(comms, reader=[], writer=StringIO([]))
        result = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": "/wt", "thread": "worker"},
            }
        )
        assert result["result"]["sessionId"] == "s1"
        assert "worker" in comms.registry

    def test_unknown_method_is_method_not_found(self, tmp_path):
        server, _ = make_server(tmp_path, [])
        response = server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope", "params": {}})
        assert response["error"]["code"] == -32601

    def test_notifications_get_no_response(self, tmp_path):
        server, _ = make_server(tmp_path, [])
        assert server.handle({"jsonrpc": "2.0", "method": "nope", "params": {}}) is None


class TestSessionPrompt:
    def _session(self, tmp_path):
        comms = wire(tmp_path)
        server = AcpServer(comms, reader=[], writer=StringIO([]))
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/new",
                "params": {"cwd": "/wt", "thread": "worker"},
            }
        )
        # A peer for the worker to receive messages from.
        from agent_comms import Thread

        comms.register(Thread(name="peer", tags=frozenset(), worktree="/wt"))
        return comms, server

    def test_prompt_broadcasts_and_drains_inbox(self, tmp_path):
        comms, server = self._session(tmp_path)
        comms.send(
            "peer",
            "worker",
            "answer",
        )
        result = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": "s1", "prompt": [{"type": "text", "text": "hello"}]},
            }
        )
        assert result["result"] == {"stopReason": "endTurn"}
        # The prompt was broadcast into the bus.
        assert comms.pending_count("peer") == 1
        # The inbox was drained via session/update notifications and acked.
        updates = [json.loads(line) for line in server._writer.written]
        chunk_updates = [
            u
            for u in updates
            if u.get("method") == "session/update"
            and u["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
        ]
        assert len(chunk_updates) == 1
        assert "peer" in chunk_updates[0]["params"]["update"]["content"]["text"]
        assert comms.pending_count("worker") == 0

    def test_prompt_text_extraction_forms(self, tmp_path):
        comms, server = self._session(tmp_path)
        assert AcpServer._prompt_text(None) == ""
        assert AcpServer._prompt_text("plain") == "plain"
        assert (
            AcpServer._prompt_text(
                [
                    {"type": "text", "text": "a"},
                    {"type": "image", "uri": "x"},
                    {"type": "text", "text": "b"},
                ]
            )
            == "a\nb"
        )

    def test_unknown_session_is_invalid_params(self, tmp_path):
        comms, server = self._session(tmp_path)
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "session/prompt",
                "params": {"sessionId": "nope", "prompt": "hi"},
            }
        )
        assert response["error"]["code"] == -32602

    def test_stopped_thread_refuses_prompt(self, tmp_path):
        comms = wire(tmp_path)
        server = AcpServer(comms, reader=[], writer=StringIO([]))
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/new",
                "params": {"cwd": "/wt", "thread": "worker"},
            }
        )
        # Tamper: stop the thread behind the server's back.
        comms.registry.unregister("worker")
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": "s1", "prompt": "hi"},
            }
        )
        assert response["error"]["code"] == -32602
        assert "stopped" in response["error"]["message"]


class TestWireProtocol:
    def test_parse_error_returns_id_null(self, tmp_path):
        reader = StringIO(["not json"])
        out = StringIO([])
        server = AcpServer(wire(tmp_path), reader=reader, writer=out)
        server.serve()
        messages = parse_written(out)
        assert messages[0]["error"]["code"] == -32700
        assert messages[0]["id"] is None

    def test_non_jsonrpc_20_rejected(self, tmp_path):
        reader = StringIO([json.dumps({"jsonrpc": "1.0", "id": 1, "method": "initialize"})])
        out = StringIO([])
        server = AcpServer(wire(tmp_path), reader=reader, writer=out)
        server.serve()
        assert parse_written(out)[0]["error"]["code"] == -32600

    def test_blank_lines_skipped(self, tmp_path):
        reader = StringIO(["", "   "])
        out = StringIO([])
        server = AcpServer(wire(tmp_path), reader=reader, writer=out)
        server.serve()
        assert out.written == []

    def test_serve_answers_each_request(self, tmp_path):
        requests = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "nope", "params": {}}),
        ]
        out = StringIO([])
        server = AcpServer(wire(tmp_path), reader=requests, writer=out)
        server.serve()
        messages = parse_written(out)
        assert [m["id"] for m in messages] == [1, 2]
        assert "result" in messages[0]
        assert "error" in messages[1]


class TestAcpSubprocess:
    def test_real_stdio_roundtrip(self, tmp_path):
        """Run the server as a real process and speak ACP to it."""
        env = dict(
            __import__("os").environ,
            AGENT_COMMS_ROOT=str(tmp_path / "wire"),
            PYTHONPATH=str(Path(__file__).parents[1] / "src"),
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent_comms.acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        new = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/new",
            "params": {"cwd": "/wt", "thread": "worker"},
        }
        prompt = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {"sessionId": "s1", "prompt": [{"type": "text", "text": "hi"}]},
        }
        try:
            out, _ = proc.communicate(
                json.dumps(init) + "\n" + json.dumps(new) + "\n" + json.dumps(prompt) + "\n",
                timeout=30,
            )
        finally:
            if proc.poll() is None:
                proc.kill()
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        responses = {m.get("id"): m for m in lines if "id" in m}
        assert responses[1]["result"]["protocolVersion"] == 1
        assert responses[2]["result"]["sessionId"] == "s1"
        assert responses[3]["result"]["stopReason"] == "endTurn"
        updates = [m for m in lines if m.get("method") == "session/update"]
        assert isinstance(updates, list)
        # Thread was registered in the wire.
        comms = wire(tmp_path / "wire")
        assert "worker" in comms.registry
