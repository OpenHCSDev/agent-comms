"""Installed Toad renders actual native WebSocket failure notifications via ACP SDK."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

from runtime_fixture import ToadApp
from toad.acp.agent import Agent

from agent_comms.acp_extension import (
    RequestFailedUpdate,
    TurnSettledUpdate,
    TurnStartedUpdate,
    decode_updates,
    encode_updates,
)


async def main():
    receipt = json.loads(Path(os.environ["PROVIDER_FAILURE_RECEIPT"]).read_text())
    assert receipt["provider_posts"] == 1
    with tempfile.TemporaryDirectory(prefix="provider-feedback-ui-", dir="/var/tmp") as directory:
        root = Path(directory)
        os.environ.update(
            AGENT_COMMS_ROOT=str(root / "wire"),
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_STATE_HOME=str(root / "state"),
            XDG_DATA_HOME=str(root / "data"),
        )
        app = ToadApp(project_dir=str(root))
        async with app.run_test(size=(130, 45)) as pilot:
            await pilot.pause()
            view = app.screen.conversation
            agent = Agent(
                root,
                {
                    "name": "Provider feedback",
                    "identity": "provider-feedback",
                    "short_name": "provider",
                    "run_command": {"*": "true"},
                    "protocol": "acp",
                },
                receipt["session_id"],
            )
            agent._message_target = view
            view.agent = agent
            view.prompt.text = "Retained draft after failure"
            failures = []
            for notification in receipt["notifications"]:
                params = notification.get("params", {})
                facts = tuple(
                    fact
                    for fact in decode_updates(params.get("update", {}).get("_meta"))
                    if isinstance(fact, (RequestFailedUpdate, TurnStartedUpdate, TurnSettledUpdate))
                )
                if not facts:
                    continue
                failures.extend(
                    fact.failure for fact in facts if isinstance(fact, RequestFailedUpdate)
                )
                await agent.server.call(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": agent.session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": ""},
                                "_meta": encode_updates(*facts),
                            },
                        },
                    }
                )
                await pilot.pause()
            assert len(failures) == 1
            assert "after the provider response stream started" in failures[0].description
            screen = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
            flat = " ".join(screen.split())
            for fragment in (
                "Provider connection failed",
                "code 1011",
                "after the provider response stream",
                "started;",
                "Started",
                "input not retried",
            ):
                assert fragment in flat, (fragment, screen)
            assert view.prompt.text == "Retained draft after failure"
            assert view.busy_count == 0
            assert agent._active_turn_id is None
            assert app._exception is None
            Path(os.environ["PROVIDER_MOUNTED_RECEIPT"]).write_text(
                json.dumps(
                    {
                        "title": failures[0].title,
                        "description": failures[0].description,
                        "input_disposition": failures[0].input_disposition,
                        "draft_retained": True,
                        "busy_count": view.busy_count,
                        "provider_requests": receipt["provider_posts"],
                        "screen": screen,
                    },
                    indent=2,
                )
            )
            await agent.stop()
    print(
        "PASS installed mounted actual native1011 feedback: title/code/stage/Started visible, "
        "draft retained, one request, no retry"
    )


if __name__ == "__main__":
    asyncio.run(main())
