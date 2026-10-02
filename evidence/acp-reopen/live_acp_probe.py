"""One fresh direct ACP prompt on the existing owner; never replay failed input."""

import asyncio
import json
import time
from pathlib import Path

from agent_comms.acp import CommsAgent
from agent_comms.active_route import read_active_route
from agent_comms.comms import wire


def wire_value(value):
    return json.loads(json.dumps(value, default=lambda item: item.model_dump(
        by_alias=True, exclude_none=True)))


async def main():
    evidence = Path(__file__).resolve().parent
    route = read_active_route()
    comms = wire()
    owner = comms.registry.require('agent-comms-ux')
    assert owner.active_turn is None and owner.goal is None
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(wire_value(kwargs['update']))

    agent = CommsAgent(
        comms, runtime_enabled=True, auto_wake=False,
        private_nk_wire_root_id=route.wire_root_id,
        private_nk_native_package=route.native_package,
    )
    agent.on_connect(Client())
    report = {'owner': owner.name, 'original_pid': owner.pid,
              'model': owner.model, 'replayed_inputs': 0, 'verified': False,
              'phase': 'attach'}
    started = time.monotonic()
    try:
        await agent.load_session(owner.worktree, owner.name)
        updates.clear()
        report['phase'] = 'fresh_prompt'
        response = await asyncio.wait_for(agent.prompt(owner.name, [{
            'type': 'text', 'text':
            'Fresh ACP startup-fix verification from Codex. This is not a retry of '
            'the earlier failed user message. Reply exactly ACP_STARTUP_FIX_OK. '
            'Do not use tools, edit files, start work, or resume previous tasks.'
        }]), timeout=180)
        assistant_text = "".join(
            update["content"].get("text", "") for update in updates
            if update.get("sessionUpdate") == "agent_message_chunk"
            and update.get("content", {}).get("type") == "text"
        )
        report["updates"] = updates
        report["assistant_text"] = assistant_text
        report.update(elapsed_seconds=time.monotonic() - started,
                      response=wire_value(response),
                      update_count=len(updates),
                      reply_received=assistant_text.strip() == 'ACP_STARTUP_FIX_OK',
                      same_owner_pid=comms.registry.require(owner.name).pid == owner.pid)
        assert report['reply_received'] and report['same_owner_pid'], report
        report['verified'] = True
    except Exception as error:
        report.update(error=repr(error), elapsed_seconds=time.monotonic() - started)
        raise
    finally:
        await agent.shutdown()
        (evidence / 'live-acp-probe.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
