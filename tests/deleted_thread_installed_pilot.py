"""Run the installed Toad UI with source Core and existing stack dependencies.

Pass a fresh disposable root; put Toad's existing tests/support on PYTHONPATH.
No provider input is submitted. The scripted ACP peer supplies session setup.
"""
import asyncio
import os
from pathlib import Path
import sys

from support.agents import peer
from support.roots import Roots
from support.workspace import Workspace

from agent_comms import native_package
from agent_comms.activity import ActivityState
from agent_comms.child_process import ProcessIdentity
from agent_comms.launch_root import LaunchRoot
from agent_comms.thread_execution import ExternalThreadExecution


async def journey(root):
    roots = Roots(root.resolve())
    environment = roots.environment(dict(os.environ))
    os.environ.clear()
    os.environ.update(environment)
    LaunchRoot.from_path(roots.wire).install()
    workspace = Workspace(roots)
    comms = workspace.comms
    for name in ('surviving', 'deleted'):
        comms.registry.declare(comms.registry.new_thread(
            name, frozenset(), str(workspace.project), execution=ExternalThreadExecution,
            process_identity=ProcessIdentity.capture(os.getpid())))
    comms.messaging.initialize_private_initial_protocol()
    async with workspace.app(agent=peer('scripted'), size=(120, 40)) as ui:
        app = ui.app
        owner = ui.selected_tab
        surviving = await ui.open_direct('surviving', actor=workspace.viewer, owner=owner)
        deleted = await ui.open_direct('deleted', actor=workspace.viewer, owner=owner)
        await ui.until(lambda: 'deleted' in app.coordination_access.thread_status.rows.rows,
                       why='both tabs have observed status')
        comms.registry.remove('deleted')
        await ui.until(lambda: app.session_navigation.get(deleted) is None,
                       why='the deleted thread tab closes')
        assert app.session_navigation.get(surviving) is not None
        await app.select_session(surviving)
        comms.agents.set_activity('surviving', ActivityState.WORKING,
                                 'P348 activity after deletion')
        await ui.until_shows('P348 activity after deletion')
        assert ui.failure() is None
        print('PASS: deleted tab closed; surviving tab remained open and displayed new activity.')
        print(ui.visible_text())
        app.save_screenshot('deleted-thread.svg', path=str(root))


if __name__ == '__main__':
    # Use the already installed reviewed native resource; do not build/install anything.
    runtime = Path(os.environ['P348_STACK_SITE'])
    native_package.MANIFEST = runtime / 'agent_comms/_native/pi-native.sha256'
    asyncio.run(journey(Path(sys.argv[1])))
