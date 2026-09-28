"""Mount the installed live route without submitting a prompt."""
import asyncio
import json
import os
from pathlib import Path


async def main():
    from agent_comms.active_route import read_active_route
    route = read_active_route()
    root = Path(__file__).resolve().parents[2] / '.artifacts/paired-live-ui'
    for key, leaf in (('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                      ('XDG_STATE_HOME', 'state')):
        os.environ[key] = str(root / leaf)
    os.environ.update(AGENT_COMMS_ROOT=str(route.root),
                      AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=route.wire_root_id,
                      AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(route.native_package))
    from toad.app import ToadApp
    from toad.widgets.comms_chat import CommsChatView
    from toad.navigation_target import DirectTarget, channel_target
    app = ToadApp(project_dir='/home/ts/.agent-comms')
    rows = []
    async with app.run_test(size=(140, 48)) as pilot:
        await pilot.pause()
        mode = app.current_mode
        targets = (channel_target('#comms'), DirectTarget('agent-comms-ux'),
                   DirectTarget('pr95-selected-pi-summary-owner'))
        for target in targets:
            await app.open_comms_session(owner_mode=mode, project_path=Path('/home/ts/.agent-comms'),
                                        me='user', target=target)
            chat = app.screen.query_one(CommsChatView)
            async with asyncio.timeout(25):
                while not chat._history_initialized or chat._refresh_lock.locked():
                    await pilot.pause(.1)
            rows.append(dict(target=target.name, initialized=True, wire_history_rows=len(chat._history)))
            print(json.dumps(rows[-1]), flush=True)
        assert rows[0]['wire_history_rows'] > 0
        assert app._exception is None
    result = dict(views=rows, prompts_sent=0, passed=True,
                  scope='mounted live wire channel/DM views; native history separately checked by ACP attachment')
    Path(__file__).with_suffix('.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    asyncio.run(main())
