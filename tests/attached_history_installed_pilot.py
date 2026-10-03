"""Normal installed fresh-fork wire view against original actual saved history.

No Node/provider call, source/store reset, patched transport or live write.
The supplied original immutable certificates are checked read-only.
History references preserve the source inodes and seals; no archive index,
registry guard or certificate is rebuilt.
"""
import asyncio
from importlib.resources import files
import json
import os
from pathlib import Path
import sys
import subprocess
import time
import hashlib
from tempfile import TemporaryDirectory

sys.path.insert(0, os.environ['TOAD_TEST_HELPERS'])
from runtime_fixture import ToadApp
from sidebar_retirement_pilot import until, viewport_text
from agent_comms.comms import wire
from agent_comms.threads import Thread
from toad.navigation_target import DirectTarget, NavigationContext, channel_target
from toad.navigation_preparation import ThreadNavigationRequest
from toad.widgets.comms_chat import CommsChatView


class InstalledApp(ToadApp):
    CSS_PATH = files('toad').joinpath('toad.tcss')


from retained_history_cutover_pilot import reference_actual_history

def prepare_physical_fixture():
    """Prepare the same original immutable archive for the existing native recorder."""
    from retained_history_cutover_pilot import reference_actual_history
    destination=Path(os.environ['PHYSICAL_FIXTURE_ROOT'])
    destination.mkdir(mode=0o700,parents=True,exist_ok=False)
    project=destination/'project';project.mkdir(mode=0o700)
    profile=destination/'profile';profile.mkdir(mode=0o700)
    (profile/'models.json').write_text(json.dumps({'providers':{'selected-offline':{
        'baseUrl':'http://127.0.0.1:9/v1','apiKey':'local-unused',
        'api':'openai-completions','models':[{'id':'fixture','contextWindow':32768,'maxTokens':2048}]}}}))
    (profile/'auth.json').write_text('{}')
    (profile/'settings.json').write_text(json.dumps({'retry':{'enabled':False,'maxRetries':0}}))
    comms=wire(destination/'wire');root_id=comms.messaging.initialize_private_initial_protocol()
    comms.registry.declare(Thread('archive-review',frozenset({'comms'}),str(project),
                                  model='selected-offline/fixture',thinking_level='off'))
    reference_actual_history(comms.root,Path(os.environ['ACTUAL_COMMS_SOURCE_ROOT']))
    index=next(i for i,item in enumerate(comms.views.historical_threads()) if item.thread.name=='nra-architecture')
    receipt={'owner':'Mendel477/268','root':str(comms.root),'wire_root_id':root_id,
             'project':str(project),'profile':str(profile),'archive_index':index,
             'input_policy':'No native/user inputs. Offline selected fixture. Provider endpoint closed.',
             'preserved_source':os.environ['ACTUAL_COMMS_SOURCE_ROOT']}
    (destination/'fixture.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt),flush=True)



async def main():
    live=Path(os.environ['ACTUAL_COMMS_SOURCE_ROOT'])
    with TemporaryDirectory(prefix='actual-archive-',dir=os.environ['TMPDIR']) as directory:
        root=Path(directory)
        os.environ.update(AGENT_COMMS_ROOT=str(root/'wire'),XDG_CONFIG_HOME=str(root/'config'),
                          XDG_STATE_HOME=str(root/'state'),XDG_DATA_HOME=str(root/'data'))
        comms=wire(root/'wire')
        comms.messaging.initialize_private_initial_protocol()
        for name, parent in [('openhcs-architecture-memory',None),('openhcs-pr159-viewer-bind-owner','openhcs-architecture-memory')]:
            comms.registry.declare(Thread(name,frozenset({'openhcs','comms'}),str(root),parent=parent,
                                          process_identity=None))
        originals=reference_actual_history(comms.root,live)
        sources=comms.bus.history.sources()
        assert [len(source.provenance.threads) for source in sources]==[104,7]
        assert [len(source.provenance.aliases) for source in sources]==[9,0]
        declared=comms.views.historical_threads()
        assert len(declared)==111
        for source in sources:
            for alias,name in source.provenance.aliases.items():
                assert source.provenance.require(alias).incarnation==source.provenance.require(name).incarnation
                assert any(item.source==source and item.thread.incarnation==source.provenance.require(name).incarnation
                           for item in comms.views.historical_threads(alias))
        print('111_ORIGINAL_DECLARATIONS_9_SOURCE_SCOPED_ALIASES_RETAINED',flush=True)
        for source in sources:
            from agent_comms.catalog_store import ChannelCatalog
            catalog=ChannelCatalog(Path(source.root)/ChannelCatalog.filename).read()
            assert '#comms' in catalog.views(source.provenance.threads)
        cli=subprocess.run([str(Path(sys.executable).with_name('agent-comms')),
            '--root',str(comms.root),'history','--channel','#comms'],
            capture_output=True,text=True,timeout=20)
        (Path(os.environ['EVIDENCE_ROOT'])/'installed-history-cli.json').write_text(cli.stdout)
        output=json.loads(cli.stdout)
        assert cli.returncode==0 and output['messages'] and 'error' not in output
        assert all('history' in message for message in output['messages'])
        print(f'INSTALLED_CLI_HISTORY_READ_{len(output["messages"])}_ORIGINAL_ROWS',flush=True)
        session=next(item for item in declared if item.thread.name=='nra-architecture')
        session_path=Path(session.thread.session_file)
        native_before=hashlib.sha256(session_path.read_bytes()).hexdigest()
        native_page=comms.transcripts.thread_transcript_page(
            session.thread.name,historical_source=session.source.key)
        assert native_page.events and native_page.after.session_file==str(session_path)
        print(f'ACTUAL_ORIGINAL_NATIVE_SESSION_PAGE_{len(native_page.events)}_EVENTS',flush=True)
        navigation=ThreadNavigationRequest(str(comms.root),'openhcs-pr159-viewer-bind-owner',root,()).read()
        assert navigation.thread.name=='openhcs-pr159-viewer-bind-owner'
        print('CANONICAL_FRESH_FORK_NAVIGATION_METADATA_LOADED',flush=True)
        me=comms.messaging.user_identity(str(root)).name
        live_bus_before=(comms.root/'bus.jsonl').read_bytes()
        app=InstalledApp(project_dir=str(root))
        try:
            await exercise_ui(app,root,me,session)
        finally:
            await asyncio.get_running_loop().shutdown_default_executor()
        assert (comms.root/'bus.jsonl').read_bytes()==live_bus_before
        assert not comms.bus.incoming_page('openhcs-pr159-viewer-bind-owner',after=0).messages
        for copied,(original,bus,checkpoint) in originals.items():
            assert (copied/'bus.jsonl').read_bytes()==bus
            assert (original/'bus.jsonl').read_bytes()==bus
            assert (original/'private_bus_checkpoint.sqlite3').read_bytes()==checkpoint
        assert hashlib.sha256(session_path.read_bytes()).hexdigest()==native_before
        print('CURRENT_PUBLIC_AND_PRIVATE_HISTORY_BYTES_PRESERVED',flush=True)
        print('NO_LIVE_BUS_WRITES_OR_HISTORICAL_WAKE_AUTHORITY',flush=True)


async def exercise_ui(app,root,me,session):
    async with app.run_test(size=(139,35)) as pilot:
        await pilot.pause(.05)
        owner=app.selected_mode
        await DirectTarget('openhcs-pr159-viewer-bind-owner').open(NavigationContext(app,owner,root,me))
        await app.selected_session.wait_content_ready()
        chat=app.selected_session.query_one(CommsChatView)
        await until(pilot,lambda: chat.message_history.initialized or 'Wire error:' in chat.status)
        assert 'Wire error:' not in chat.status,chat.status
        assert chat.message_history.initialized and chat.prompt.prompt_text_area.has_focus
        print('INSTALLED_FRESH_FORK_WIRE_LOAD_NO_SCHEMA_ERROR',flush=True)
        await channel_target('#comms').open(NavigationContext(app,owner,root,me))
        await app.selected_session.wait_content_ready()
        historical=app.selected_session.query_one(CommsChatView)
        await until(pilot,lambda: bool(historical.message_history.rows) or 'Wire error:' in historical.status)
        assert 'Wire error:' not in historical.status,historical.status
        historical.window.scroll_end(animate=False,immediate=True)
        message=historical.message_history.rows[-1][0]
        words=[word[:12] for word in message.body.split() if len(word)>=8]
        assert words,'The actual source message must have a visible text witness'
        await until(pilot,lambda: any(word in viewport_text(historical) for word in words))
        assert not message.starts_turn
        app.save_screenshot('historical-channel.svg',path=os.environ['EVIDENCE_ROOT'])
        # Follow the real Saved sessions button, then select the original identity.
        from textual.widgets import Button
        button=app.selected_session.query_one('#historical-sessions',Button)
        await pilot.pause(.1)
        def flags():
            return {'selected_mode':app.selected_mode,'screen_type':type(app.screen).__name__,
                    'screen_id':app.screen.id,'pending_mode':app._pending_mode_switch,
                    'atomic_mode':app._atomic_mode_switch,'button_region':repr(button.region),
                    'button_visible':button.visible,'button_display':button.display,
                    'button_on_screen':button.is_on_screen,'button_screen_current':button.screen.is_current}
        before=flags()
        clicked=await pilot.click(button,offset=(button.region.width//2,button.region.height//2))
        (Path(os.environ['EVIDENCE_ROOT'])/'native-control-flags.json').write_text(json.dumps(
            {'before_saved_click':before,'clicked':clicked,'after_saved_click':flags()},indent=2))
        assert clicked,before
        from toad.screens.historical_sessions import HistoricalSessions
        from textual.widgets import Select, Static
        from toad.widgets.transcript_history import TranscriptHistory
        await until(pilot,lambda: isinstance(app.screen,HistoricalSessions))
        screen=app.screen
        index=next(index for index,item in enumerate(screen.threads)
                   if item.thread.incarnation==session.thread.incarnation and item.source==session.source)
        selector=screen.query_one('#saved-identity',Select)
        await pilot.pause(.1)
        await pilot.click(selector,offset=(selector.region.width//2,selector.region.height//2))
        # Select's native hit is its SelectCurrent child. Pilot's exact-widget
        # hit boolean is not the control's opening result.
        await until(pilot, lambda: selector.expanded)
        await pilot.press('home',*(['down']*index),'enter')
        await until(pilot,lambda: screen.query_one('#saved-identity',Select).value==index and
                    'nra-architecture' in str(screen.query_one('#source-label',Static).content) and
                    bool(list(screen.query(TranscriptHistory))))
        await pilot.pause(.1)
        app.save_screenshot('historical-native-selection-attempt.svg',path=os.environ['EVIDENCE_ROOT'])
        assert list(screen.query(TranscriptHistory)), [str(widget.content) for widget in screen.query(Static)]
        app.save_screenshot('historical-native-session.svg',path=os.environ['EVIDENCE_ROOT'])
        assert 'nra-architecture' in viewport_text(screen)
        await pilot.press('escape')
        await until(pilot,lambda: app.selected_session.query_one(CommsChatView) is historical)
        assert app._exception is None
        print('REAL_SAVED_SESSIONS_KEY_ORIGINAL_NATIVE_SELECT_PAINT_RETURN_PASS',flush=True)
        print('ACTUAL_ARCHIVED_PUBLIC_MESSAGE_PAINTED_NO_DELIVERY_AUTHORITY',flush=True)

if __name__=='__main__':
    if '--prepare-physical' in sys.argv:
        prepare_physical_fixture()
    else:
        started=time.monotonic()
        asyncio.run(main())
        print(f'INSTALLED_CONTINUOUS_JOURNEY_PASS_SECONDS={time.monotonic()-started:.3f}',flush=True)
