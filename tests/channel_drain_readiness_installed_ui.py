"""Actual installed TUI/native receiver failure and independent saved input.

Reuse the existing application/native journey. Only the localhost provider HTTP
status is controlled; UI, registry, source, ACP and native processes remain real. The application runs
under Textual Pilot; external st/Linux entrypoint acceptance is a separate gate.
"""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler
from importlib.resources import files
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from unittest.mock import patch

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--toad-tests', type=Path, required=True)
parser.add_argument('--stage', type=Path, required=True)
arguments = parser.parse_args()
sys.path.insert(0, str(arguments.toad_tests))
from l0a_native_installed_pilot import main, until, response_painted
from runtime_fixture import ToadApp
from saved_state_user_journey_pilot import click_tab, screen_paint
from toad.navigation_target import channel_target, NavigationContext
from toad.widgets.comms_chat import CommsChatView
from toad.widgets.message_notifications import MessageNotifications
from toad.widgets.session_details import SessionDetails
from agent_comms.activity import UnavailableDrainReadiness
from agent_comms.threads import Thread
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.native_runtime_input import NativeRuntimeInput
import sqlite3
from contextlib import closing

provider_failure = False
original_status = BaseHTTPRequestHandler.send_response

def provider_status(handler, status, *args, **kwargs):
    # This patch is confined to the controlled HTTP provider. Its genuine 503
    # response exercises native SDK failure and the original drain producer.
    return original_status(handler, 503 if provider_failure else status, *args, **kwargs)

class InstalledApp(ToadApp):
    CSS_PATH = files('toad').joinpath('toad.tcss')

async def prepare(comms, project, *_):
    comms.registry.declare(Thread('history-sender', frozenset(), str(project)))
    comms.registry.declare(Thread('history-recipient', frozenset(), str(project)))
    for index in range(220):
        comms.messaging.send_initial_cohort('history-sender', 'history-recipient',
            f'Original retained source {index}: ' + 'history '*250)

async def acceptance(app, pilot, agent, comms, entered, release, hold_next, requests):
    global provider_failure
    started = perf_counter()
    evidence = Path(os.environ['L0A_EVIDENCE'])
    view = app.selected_session.conversation
    original_tab = app.selected_session.id
    mode = app.selected_mode
    release.set(); hold_next.clear()
    await until(pilot, lambda: view.agent_ready)
    view.prompt.text = 'NEW_PRIVATE_ORDINARY_FIRST_REPLY'
    view.prompt.prompt_text_area.focus()
    await pilot.press('enter')
    await until(pilot, lambda: response_painted(app, view, 'NATIVE_RESPONSE_1'))
    await until(pilot, lambda: not comms.registry.require('beta').executing)
    assert len(requests) == 1
    (evidence/'first-reply.svg').write_text(app.export_screenshot())
    user = comms.messaging.user_identity(str(agent.project_root_path)).name
    await channel_target('#team').open(NavigationContext(app, mode, agent.project_root_path, user))
    await app.selected_session.wait_content_ready()
    channel = app.selected_session.query_one(CommsChatView)
    channel_tab = app.selected_session.id
    provider_failure = True
    channel.prompt.text = 'NEW_PRIVATE_CONTROLLED_PROVIDER_FAILURE'
    channel.prompt.prompt_text_area.focus()
    await pilot.press('enter')
    await until(pilot, lambda: isinstance(comms.agents.activity_of('beta').readiness,
                                        UnavailableDrainReadiness), 25)
    assert len(requests) == 2
    failure_source = next(message for message in comms.bus.channel_history('#team')
                          if message.body == 'NEW_PRIVATE_CONTROLLED_PROVIDER_FAILURE')
    with closing(sqlite3.connect((comms.root/'coordination.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        retained_input, = NativeRuntimeInput.select(db)
    # Existing native failure stopped the original drain. A fresh independent
    # input is saved, not sent/retried because another input is uncertain.
    channel.prompt.text = 'NEW_PRIVATE_INDEPENDENT_PENDING_CHANNEL'
    channel.prompt.prompt_text_area.focus()
    await pilot.press('enter')
    await until(pilot, lambda: any(message.body == 'NEW_PRIVATE_INDEPENDENT_PENDING_CHANNEL'
                                 for message in comms.bus.channel_history('#team')))
    pending_source = next(message for message in comms.bus.channel_history('#team')
                          if message.body == 'NEW_PRIVATE_INDEPENDENT_PENDING_CHANNEL')
    await until(pilot, lambda: any('Waiting for recovery' in str(item.title)
                                 for item in channel.query(MessageNotifications)))
    notification = next(item for item in channel.query(MessageNotifications)
                        if 'Waiting for recovery' in str(item.title))
    notification.scroll_visible(animate=False, immediate=True)
    await pilot.pause()
    assert await pilot.click(notification.query_one('CollapsibleTitle'))
    await until(pilot, lambda: 'Waiting for recovery' in screen_paint(app))
    assert notification in app.screen._compositor.visible_widgets
    channel_frame = screen_paint(app)
    (evidence/'channel-recovery.txt').write_text(channel_frame)
    (evidence/'channel-recovery.svg').write_text(app.export_screenshot())
    await click_tab(app, pilot, original_tab)
    details = app.selected_session.query_one(SessionDetails)
    await until(pilot, lambda: 'Waiting for recovery' in str(details.title))
    details.scroll_visible(animate=False, immediate=True)
    await pilot.pause()
    assert await pilot.click(details.query_one('CollapsibleTitle'))
    await until(pilot, lambda: 'Inbox unavailable' in screen_paint(app))
    assert details in app.screen._compositor.visible_widgets
    dm_frame = screen_paint(app)
    (evidence/'dm-recovery.txt').write_text(dm_frame)
    (evidence/'dm-recovery.svg').write_text(app.export_screenshot())
    await click_tab(app, pilot, channel_tab)
    await until(pilot, lambda: 'Waiting for recovery' in screen_paint(app))
    with closing(sqlite3.connect((comms.root/'coordination.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        assert NativeRuntimeInput.one(db,input_id=retained_input.input_id) == retained_input
        rows = WakeAssignment.select(db)
    assert len(requests) == 2
    assert any(row.wire_seq==failure_source.seq and row.lifecycle.deferred for row in rows)
    assert not any(row.wire_seq == pending_source.seq for row in rows)
    original, = comms.bus.log.deliveries_for_references((pending_source.reference,))
    assert original.message == pending_source
    assert any(recipient.canonical_thread == 'beta' for recipient in original.audience.recipients)
    result = {'elapsed_seconds':perf_counter()-started, 'localhost_provider_posts':len(requests),
        'ui_driver':'Textual App.run_test/Pilot', 'external_terminal_entrypoint_verified':False,
        'ordinary_first_reply_visible':True, 'actual_native_provider_failure':True,
        'same_open_ui_channel_waiting_recovery_visible':True,
        'same_owner_dm_waiting_recovery_title':str(details.title),
        'actual_click_return_preserved_recovery':True,
        'failed_original_native_input_unchanged':retained_input.input_id,
        'independent_original_frozen_source_without_handling_claim':pending_source.seq,
        'original_replays':0,'public_mutations':0,'paid_provider_calls':0}
    (evidence/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result),flush=True)

if __name__ == '__main__':
    with patch.object(BaseHTTPRequestHandler,'send_response',provider_status):
        asyncio.run(main(app_type=InstalledApp, acceptance=acceptance, prepare_state=prepare,
            fixture_stage=arguments.stage, provider_request_budget=2,
            native_settings={'retry':{'enabled':False,'maxRetries':0},'compaction':{'enabled':False}},
            headless=False))
