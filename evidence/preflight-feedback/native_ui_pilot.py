"""Actual pinned Pi import-boundary startup failure -> durable receipt -> normal painted UI."""
import asyncio
import os
from pathlib import Path
from l0a_native_installed_pilot import main, until
from context_native_installed_pilot import InstalledApp
from toad import messages
from agent_comms.input_disposition import InputDispositions
from agent_comms.input_attempt import NotSentInput

async def prepare(comms, project, *unused):
    extension = project / 'blocked-startup.ts'
    extension.write_text('export default async function () { process.stderr.write("ACTUAL_NATIVE_STARTUP_BLOCKED\\n"); await new Promise(() => {}); }\n')
    os.environ['AGENT_COMMS_AGENT_ARGS'] = '--provider selected-offline --model fixture --no-skills --no-context-files'

async def acceptance(app, pilot, agent, comms, entered, release, hold_next, requests):
    view = app.selected_session.conversation
    import json
    config = Path(os.environ['PI_CODING_AGENT_DIR'])
    (config / 'settings.json').write_text(json.dumps({'extensions': [str(Path(comms.registry.require('beta').worktree) / 'blocked-startup.ts')]}))
    await until(pilot, lambda: view.agent_ready)
    view.prompt.text = 'PRIVATE_PREFLIGHT_INPUT_NEVER_SENT'
    view.prompt.prompt_text_area.focus()
    await pilot.press('enter')
    def painted():
        return '\n'.join(strip.text for strip in app.screen._compositor.render_strips())
    await until(pilot, lambda: 'Native import boundary refused' in painted(), 35)
    await until(pilot, lambda: not comms.registry.require('beta').executing)
    frame = painted()
    assert 'Not sent' in frame and 'input not retried' in frame, frame
    assert 'Unknown' not in frame, frame
    rows = InputDispositions(comms.root / InputDispositions.filename).read().rows
    assert len(rows) == 1 and all(isinstance(row, NotSentInput) for row in rows.values()), rows
    assert not requests, 'Preflight failure must never call provider'
    assert app._exception is None
    root = Path(os.environ['L0A_EVIDENCE'])
    (root / 'paint.txt').write_text(frame)
    (root / 'feedback.svg').write_text(app.export_screenshot())
    print('ACTUAL_NATIVE_EXIT_STDERR_NOT_SENT_PAINT_ZERO_PROVIDER', flush=True)

if __name__ == '__main__':
    asyncio.run(main(app_type=InstalledApp, acceptance=acceptance, prepare_state=prepare))
