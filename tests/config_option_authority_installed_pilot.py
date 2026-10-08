"""Real private owner/socket/catalog journey, without a model prompt.

Run with the candidate wheel installed in its owned target directory. Reuse the
authentic native model configuration for read-only catalog queries. No public
root, setting, native journal or historical input is changed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

from agent_comms.comms import Comms
from agent_comms.pi_vocabulary import OffThinkingLevel, HighThinkingLevel
from agent_comms.runtime import socket_path
from agent_comms.runtime_requests import SubscribeRuntimeRequest, SetConfigOptionRuntimeRequest
from agent_comms.threads import Thread


async def exchange(service, name, request):
    owner = service.registry.require(name)
    async with asyncio.timeout(15):
        while not socket_path(service.root, owner.pid).exists():
            assert owner.process_alive
            await asyncio.sleep(.02)
        reader, writer = await asyncio.open_unix_connection(
            socket_path(service.root, owner.pid), limit=8 * 1024 * 1024
        )
        try:
            writer.write((json.dumps(request.to_wire()) + '\n').encode())
            await writer.drain()
            while line := await reader.readline():
                packet = json.loads(line)
                if 'ready' in packet or 'result' in packet or 'error' in packet:
                    return packet
            raise AssertionError('Owner closed before the requested receipt')
        finally:
            writer.close()
            await writer.wait_closed()


def thinking(options):
    return next(option for option in options if option['id'] == 'thinking_level')


async def visible_configuration(stage, project, service, name, *, menu_contention=False):
    from toad.agent_schema import AgentDefinition
    from toad.app import ToadApp
    import shlex

    os.environ.update(
        XDG_CONFIG_HOME=str(stage / 'ui-config'), XDG_STATE_HOME=str(stage / 'ui-state'),
        XDG_DATA_HOME=str(stage / 'ui-data'),
    )
    definition = AgentDefinition.decode({
        'name': 'Configuration authority', 'identity': 'configuration-authority',
        'short_name': 'config', 'protocol': 'acp',
        'run_command': {'*': shlex.join([sys.executable, '-m', 'agent_comms.acp'])},
    })
    app = ToadApp(agent_data=definition, project_dir=str(project), agent_session_id=name)
    async with app.run_test(size=(120, 40)) as pilot:
        async with asyncio.timeout(15):
            while True:
                await pilot.pause(.05)
                view = app.selected_session.conversation
                if view is None or view.agent is None:
                    continue
                if view.agent.configuration.thinking.current != 'off':
                    continue
                paint = '\n'.join(strip.text for strip in app.screen._compositor.render_strips())
                if ' · off' in paint:
                    break
        assert service.registry.require(name).thinking_level is OffThinkingLevel
        app.save_screenshot(str(stage / 'configured-off.svg'))
        if menu_contention:
            import sqlite3
            from time import monotonic
            from toad.core.input_events import ChangeModel
            from toad.db import DB, MODEL_HISTORY_SCHEMA
            from toad.widgets.comms_menu import ContextMenu

            database = DB()
            def acquire_writer():
                writer = sqlite3.connect(database.path, check_same_thread=False)
                writer.execute(MODEL_HISTORY_SCHEMA)
                writer.commit()
                writer.execute('BEGIN IMMEDIATE')
                return writer

            writer = await asyncio.to_thread(acquire_writer)
            try:
                started = monotonic()
                view.publish_core(ChangeModel(view.agent.configuration.model.current))
                async with asyncio.timeout(4):
                    while not isinstance(app.screen, ContextMenu):
                        await pilot.pause(.01)
                menu_seconds = monotonic() - started
                assert writer.in_transaction
                app.save_screenshot(str(stage / 'thinking-menu-held-writer.svg'))
                await pilot.press('down', 'escape')
                assert not isinstance(app.screen, ContextMenu)
                assert writer.in_transaction
            finally:
                await asyncio.to_thread(writer.rollback)
                await asyncio.to_thread(writer.close)
            async with asyncio.timeout(4):
                while view.agent.configuration.model.current not in await database.recent_models(view.model_history_scope):
                    await pilot.pause(.01)
            return {'actual_installed_toad_acp': True, 'thinking_menu_seconds': menu_seconds,
                    'menu_opened_with_writer_held': True, 'keyboard_dismissed_with_writer_held': True,
                    'recent_model_persisted_after_release': True}
    return {'actual_installed_toad_acp': True, 'configured_off_visibly_painted': True,
            'followup_input_required': False}


async def run(stage, package, models, ui, menu_contention=False):
    stage.mkdir(mode=0o700, parents=True, exist_ok=False)
    project = stage / 'project'
    project.mkdir()
    service = Comms(stage / 'wire')
    root_id = service.messaging.initialize_private_initial_protocol()
    service.owners.pin_private_nk_launch(service.root, root_id, package)
    environment = dict(os.environ)
    environment.update(
        AGENT_COMMS_ROOT=str(service.root), AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
        AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
    )
    for key in ('PI_PROMPT', 'PI_PARENT_ID', 'PI_TASK', 'PI_AGENT_ID',
                'AGENT_COMMS_STARTUP_INPUT_KEY', 'AGENT_COMMS_AGENT_MODELS'):
        environment.pop(key, None)
    os.environ.clear()
    os.environ.update(environment)
    rows = []
    for index, model in enumerate(models):
        name = f'configuration-{index}'
        service.registry.declare(Thread(
            name, frozenset(), str(project), model=model, thinking_level=OffThinkingLevel,
            task='Private provider-free configuration acceptance; never send a prompt',
        ))
        service.owners.start(
            name, agent_bin=str(Path(sys.executable).with_name('pi-comms-native')),
            agent_args=[],
        )
        owner = service.registry.require(name)
        try:
            ready = await exchange(service, name, SubscribeRuntimeRequest(thread=name))
            assert 'ready' in ready, ready
            advertised = next(option for option in ready['ready']['configOptions']
                              if option['id'] == 'model')
            assert len(advertised['options']) > 1, advertised
            assert model in {choice['value'] for choice in advertised['options']}
            if menu_contention:
                visible = await visible_configuration(stage, project, service, name, menu_contention=True)
                rows.append({'model': model, **visible})
                continue
            selected = await exchange(service, name, SetConfigOptionRuntimeRequest(
                thread=name, config_id='model', value=model,
            ))
            assert 'result' in selected, selected
            assert service.registry.require(name).model == model
            option = thinking(ready['ready']['configOptions'])
            assert option['currentValue'] == 'off'
            assert service.registry.require(name).thinking_level is OffThinkingLevel
            before = (service.root / 'registry.json').read_bytes()
            again = await exchange(service, name, SubscribeRuntimeRequest(thread=name))
            assert thinking(again['ready']['configOptions'])['currentValue'] == 'off'
            assert (service.root / 'registry.json').read_bytes() == before
            visible = await visible_configuration(stage, project, service, name) if ui else {}
            # The view retains a configured selection even when it is no longer
            # available. That display-only value cannot authorize a new command.
            refused = await exchange(service, name, SetConfigOptionRuntimeRequest(
                thread=name, config_id='thinking_level', value='off',
            ))
            assert 'rpcError' in refused, refused
            assert service.registry.require(name).thinking_level is OffThinkingLevel
            changed = await exchange(service, name, SetConfigOptionRuntimeRequest(
                thread=name, config_id='thinking_level', value='high',
            ))
            assert 'result' in changed, changed
            assert service.registry.require(name).thinking_level is HighThinkingLevel
            final = await exchange(service, name, SubscribeRuntimeRequest(thread=name))
            assert thinking(final['ready']['configOptions'])['currentValue'] == 'high'
            assert service.registry.require(name).model == model
            assert service.registry.require(name).active_turn is None
            assert not list((service.root / 'native-sessions').rglob('*.jsonl'))
            rows.append({
                'model': model, 'startup_configured_off_retained': True,
                'repeat_subscription_registry_sha256': hashlib.sha256(before).hexdigest(),
                'read_did_not_write': True, 'unsupported_explicit_selection_refused': True,
                'supported_explicit_high_persisted': True,
                **visible,
            })
        finally:
            service.owners.stop(name)
        assert not owner.process_alive
    receipt = {'models': rows, 'real_worker_socket_catalog': True,
               'prompt_commands': 0, 'provider_calls': 0,
               'public_mutations': 0, 'all_owned_workers_retired': True}
    (stage / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--model', action='append', required=True)
    parser.add_argument('--ui', action='store_true')
    parser.add_argument('--menu-db-contention', action='store_true')
    args = parser.parse_args()
    assert args.stage.is_relative_to('/home/ts/wt')
    print(json.dumps(asyncio.run(run(args.stage, args.package, args.model, args.ui, args.menu_db_contention)), indent=2))


if __name__ == '__main__':
    main()
