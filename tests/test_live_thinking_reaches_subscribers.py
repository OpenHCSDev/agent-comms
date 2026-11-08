"""A bus-started native turn streams its thinking and reply to an attached viewer while it runs.

Bus messages run as selected turns, not owned turns. They once handed the runtime's live
publication only its compaction handler, so a viewer (Toad) saw nothing until history committed.
"""

import asyncio
from contextlib import AsyncExitStack

from agent_comms.projects.checkouts import Checkout
from delivery_owner_fixture import canonical_agent, reply_turn_finished
from native_backend_fixture import native_backend_fixture


async def test_bus_turn_thinking_and_reply_reach_a_subscribed_viewer_live(tmp_path, native_runtime_package):
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native:
        await native.author_history()
        async with AsyncExitStack() as lifetime:
            agent, session = await lifetime.enter_async_context(
                native.open_owner(auto_wake=True))
            comms = agent._comms
            viewer = canonical_agent(comms, auto_wake=False)
            received = []

            class Viewer:
                async def publish_update(self, session_id, update):
                    received.append(update.model_dump(mode="json", by_alias=True, exclude_none=True))
                    return True

            viewer.on_connect(Viewer())
            proxy = viewer._create_runtime_proxy(comms.registry.require(session), session)
            await proxy.subscribe()
            lifetime.push_async_callback(viewer.shutdown)
            lifetime.push_async_callback(proxy.close)

            comms.threads.claim_thread('sender', tags=frozenset(), worktree=Checkout(native.project))
            native.provider.thinking = 'Checking each power of five in turn.'
            native.provider.text = 'Two hundred forty nine.'
            comms.messaging.send('sender', session, 'How many trailing zeros does 1000! have?')
            async with asyncio.timeout(60):
                await agent.inputs.drain_inbox(session)
                await reply_turn_finished(agent, session)
                while not any(update.get("sessionUpdate") == "agent_thought_chunk" for update in received):
                    await asyncio.sleep(0.05)

            def streamed(kind):
                return "".join(update["content"]["text"] for update in received
                               if update.get("sessionUpdate") == kind)

            assert streamed("agent_thought_chunk") == 'Checking each power of five in turn.'
            assert 'Two hundred forty nine.' in streamed("agent_message_chunk")
