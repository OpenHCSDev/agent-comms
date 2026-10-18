"""Actual saved Pi -> native extension UI -> owner ACP socket -> same Pi reply."""

import asyncio
import json
from types import SimpleNamespace

from agent_comms.acp_extension import McpClientReceiptUpdate, decode_updates
from agent_comms.comms import Comms
from agent_comms.runtime import RuntimeProxy
from delivery_owner_fixture import canonical_agent
from native_event_host import install_event_host

pytest_plugins = ("test_backend_native_lifecycle",)


async def test_actual_saved_native_ui_receipt_and_controller_lifetime(native_backend, monkeypatch):
    fixture = native_backend
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "response-local/fixture")
    owner = canonical_agent(
        Comms(fixture.root),
        agent_bin="pi",
        agent_args=[
            "--provider",
            "response-local",
            "--model",
            "fixture",
            "--thinking",
            "off",
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--no-prompt-templates",
            "--no-tools",
        ],
        runtime_enabled=True,
        auto_wake=False,
    )
    updates, permissions = [], []

    class Attachment:
        async def session_update(self, **kwargs):
            updates.extend(decode_updates(kwargs["update"].get("_meta")))

        async def request_permission(self, **kwargs):
            permissions.append(kwargs)
            options = kwargs["options"]
            choice = (
                "choice-1" if any(o["optionId"] == "choice-1" for o in options) else "allow-once"
            )
            return {"outcome": {"outcome": "selected", "optionId": choice}}

    proxy = None
    try:
        await owner.new_session(cwd=str(fixture.project), mcp_servers=[])
        attached = SimpleNamespace(
            _comms=owner._comms,
            sessions=SimpleNamespace(
                transcript=SimpleNamespace(snapshots=False, diffs=False),
                client=Attachment(),
            ),
        )
        proxy = RuntimeProxy(attached, "project", owner._runtime.path)
        await proxy.subscribe()
        async with asyncio.timeout(25):
            seeded = await proxy.request(
                "prompt", prompt=[{"type": "text", "text": "Seed saved context"}]
            )
        assert seeded["stopReason"] == "end_turn", seeded
        saved = owner._comms.registry.require("project").session_file
        assert saved
        await owner.turns.persistent_backends["project"].close_idle()
        config = json.loads((fixture.config / "models.json").read_text())
        origin = config["providers"]["response-local"]["baseUrl"].removesuffix("/v1")
        probe = fixture.project / "actual-ui-replies.jsonl"
        install_event_host(
            monkeypatch,
            "pi",
            origin,
            ui_probe=probe,
            native_settings={"compaction": {"enabled": False}, "retry": {"enabled": False}},
        )
        async with asyncio.timeout(25):
            replied = await proxy.request(
                "prompt", prompt=[{"type": "text", "text": "Check current controller"}]
            )
        assert replied["stopReason"] == "end_turn", replied
        rows = [json.loads(line) for line in probe.read_text().splitlines()]
        assert len(rows) == 1
        assert rows[0] == {
            "inputId": rows[0]["inputId"],
            "confirmed": True,
            "selected": "two",
            "input": None,
            "edited": None,
        }
        assert len(permissions) == 2
        receipts = [update for update in updates if isinstance(update, McpClientReceiptUpdate)]
        assert len(receipts) == 1 and receipts[0].receipt.input_id == rows[0]["inputId"]
        assert owner._comms.registry.require("project").session_file == saved
        await proxy.close()
        proxy = None
        async with asyncio.timeout(25):
            denied = await owner.prompt("project", [{"type": "text", "text": "No controller"}])
        assert denied.stop_reason == "end_turn", denied
        rows = [json.loads(line) for line in probe.read_text().splitlines()]
        assert len(rows) == 2
        assert rows[1] == {
            "inputId": rows[1]["inputId"],
            "confirmed": False,
            "selected": None,
            "input": None,
            "edited": None,
        }
        assert rows[0]["inputId"] != rows[1]["inputId"]
        assert len(permissions) == 2 and fixture.provider.posts == 3
        from pathlib import Path

        saved_rows = [json.loads(line) for line in Path(saved).read_text().splitlines()]
        users = [row for row in saved_rows if row.get("message", {}).get("role") == "user"]
        assert len(users) == 3
        assert len({row["message"]["inputId"] for row in users}) == 3
    finally:
        if proxy is not None:
            await proxy.close()
        await owner.shutdown()
