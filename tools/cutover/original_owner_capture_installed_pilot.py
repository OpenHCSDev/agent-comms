"""Provider-free genuine original admission, strict target capture and refusal."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from original_owner_capture import OriginalTypedCapture
from agent_comms.goal_history import GoalHistoryStore


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def runtime_replacement():
    """Actual installed source workers, stopped publication and ACP reattachment."""
    import asyncio
    from dataclasses import replace
    import sys
    from acp import spawn_agent_process
    import agent_comms
    from agent_comms.active_route import ActiveRoute, active_route_path, publish_active_route
    from agent_comms.comms import Comms
    from agent_comms.field_codec import FieldCodec
    from agent_comms.owner_cutover import PreserveOwnerRuntime
    from agent_comms.owner_launch import RetainedOwnerLaunch
    from native_schema_carry import NativeSchemaDeclaration
    import publish_retained_summary as publication
    import publish_openhcs_recovery as original_publication
    from runtime_installation import PreserveRuntimeInstallation
    from seed_thread_retirement_fixture import ready

    output = Path(os.environ['AC_REPLACEMENT_OUTPUT'])
    output.mkdir(mode=0o700)
    source = Path(os.environ['AC_REPLACEMENT_SOURCE'])
    target = Path(sys.executable).parent.parent
    assert Path(agent_comms.__file__).is_relative_to(target)
    package = Path(os.environ['PI_COMPACTION_TEST_PACKAGE'])
    home = output / 'home'
    links = home / '.local/bin'
    links.mkdir(mode=0o700, parents=True)
    os.environ['HOME'] = str(home)
    route_path = active_route_path()
    route_path.parent.mkdir(mode=0o700, parents=True)
    for command in publication.COMMANDS:
        (links / command).symlink_to(source / 'bin' / command)
    root = output / 'wire'
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.startswith('AGENT_COMMS_') or name.startswith('PI_'):
            environment.pop(name)
    environment['PI_COMPACTION_TEST_PACKAGE'] = str(package)
    environment['PYTHONPATH'] = str(Path(__file__).parents[2] / 'tests') + ':/usr/lib/python3.14/site-packages'
    log = (output / 'source.stderr.log').open('w')
    process = subprocess.Popen((str(source / 'bin/python'),
        str(Path(__file__).with_name('seed_original_owner_capture.py')),
        '--runtime-owners', str(root), str(package)),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, env=environment)
    service = None
    try:
        line = await asyncio.to_thread(process.stdout.readline)
        assert line, 'Source fixture failed; see source.stderr.log'
        original = json.loads(line)
        route = ActiveRoute(root, original['root_id'], package)
        publish_active_route(route, route_path)
        os.environ.update(AGENT_COMMS_ROOT=str(root),
            AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=original['root_id'],
            AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package))
        service = Comms(root)
        service.owners.pin_private_nk_launch(root, original['root_id'], package)
        names = original['names']
        before = service.registry.snapshot()
        originals = tuple(before.require(name) for name in names)
        launches = tuple(RetainedOwnerLaunch.capture(owner, before,
                         interpreter=str(source / 'bin/python')) for owner in originals)
        sessions = {owner.session_file: digest(Path(owner.session_file)) for owner in originals}

        class Subscriber:
            def __init__(self):
                self.updates = []
            def on_connect(self, connection):
                pass
            async def session_update(self, **kwargs):
                self.updates.append(kwargs['update'].model_dump(by_alias=True, exclude_none=True))
            async def request_permission(self, **kwargs):
                raise AssertionError('History attachment must not request permission')

        async def attach(interpreter, phase):
            updates = {}
            for owner in originals:
                subscriber = Subscriber()
                async with spawn_agent_process(subscriber, str(interpreter), '-m', 'agent_comms.acp',
                        env={k:v for k,v in os.environ.items() if k != 'PYTHONPATH'},
                        cwd=owner.worktree) as (client, child):
                    await client.initialize(protocol_version=1, client_capabilities={})
                    await client.load_session(cwd=owner.worktree, session_id=owner.name, mcp_servers=[])
                    assert any('Original saved answer' in json.dumps(row) for row in subscriber.updates)
                    updates[owner.name] = subscriber.updates
            (output / (phase + '-acp.json')).write_text(json.dumps(updates, indent=2)+'\n')

        await attach(source / 'bin/python', 'before')
        publication.ROOT, publication.LINKS = root, links
        original_publication.ROOT, original_publication.LINKS = root, links
        def artifact(path):
            return publication.ReviewedArtifact(path, digest(path))
        cohort = publication.ReviewedCommsBackendCohort(target, source, route, package,
            artifact(Path(os.environ['AC_REPLACEMENT_ACTIVATION'])),
            artifact(Path(os.environ['AC_REPLACEMENT_PROOF'])),
            (artifact(Path(os.environ['AC_REPLACEMENT_GATE'])),), source)
        results = await asyncio.to_thread(publication.publish, cohort, PreserveOwnerRuntime(),
            PreserveRuntimeInstallation(NativeSchemaDeclaration.observe().goal), output / 'publication.json')
        after = service.registry.snapshot()
        for owner, launch in zip(originals, launches, strict=True):
            current = after.require(owner.name)
            assert not owner.process_alive
            assert current.process_identity != owner.process_identity
            assert replace(current, process_identity=owner.process_identity) == owner
            retained = RetainedOwnerLaunch.capture(current, after, interpreter=str(target / 'bin/python'))
            assert retained.arguments == launch.arguments
            assert retained.environment['BATCH_OWNER_CREDENTIAL'] == launch.environment['BATCH_OWNER_CREDENTIAL']
            assert retained.environment['PI_CODING_AGENT_DIR'] == launch.environment['PI_CODING_AGENT_DIR']
            await ready(root, current)
        await attach(target / 'bin/python', 'after')
        assert {path:digest(Path(path)) for path in sessions} == sessions
        assert (links / 'toad').readlink() == source / 'bin/toad'
        receipt = {'passed':True, 'source':str(source), 'target':str(target),
            'results':FieldCodec.encode(results), 'saved_history_bytes_preserved':True,
            'settings_preserved':True, 'actual_source_and_target_ACP_history':True,
            'frontend_unchanged':True, 'provider_inputs':0, 'public_mutations':0}
        (output / 'receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
        print(json.dumps(receipt), flush=True)
    finally:
        if service is not None:
            for owner in service.registry.snapshot().threads.values():
                if owner.process_alive:
                    await asyncio.to_thread(service.owners.stop, owner.name)
        process.stdin.close()
        await asyncio.to_thread(process.wait, timeout=10)
        log.close()


def main():
    stage = Path(os.environ['AC_CAPTURE_FIXTURE_STAGE'])
    assert stage.is_relative_to('/home/ts/wt')
    stage.mkdir(mode=0o700, parents=True)
    root = stage / 'original'
    original_python = Path(os.environ['AC_CAPTURE_ORIGINAL_PYTHON'])
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.update(AGENT_COMMS_THREAD='captured-original',
                       AGENT_COMMS_AGENT_BIN=str(original_python.parent / 'pi-comms-native'),
                       AGENT_COMMS_AGENT_ARGS='--fixture-private "original settings"',
                       ORIGINAL_CAPTURE_CREDENTIAL='RAM-only-private-fixture')
    process = subprocess.Popen([
        str(original_python), str(Path(__file__).with_name('seed_original_owner_capture.py')),
        str(root),
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=environment)
    try:
        ready = process.stdout.readline()
        assert ready, process.stderr.read()
        assert json.loads(ready)['pid'] == process.pid
        original_registry = digest(root / 'registry.json')
        original_releases = digest(root / 'owner_release_receipts.json')
        original_history = digest(root / 'goal_history.sqlite3')
        reader = OriginalTypedCapture(root, original_python)
        captured = reader.read('captured-original')
        assert captured.retained.process.pid == process.pid
        assert captured.retained.arguments == ('--fixture-private', 'original settings')
        assert captured.retained.environment['ORIGINAL_CAPTURE_CREDENTIAL'] == 'RAM-only-private-fixture'
        assert captured.require_current().incarnation == captured.source.incarnation
        assert original_registry == digest(root / 'registry.json')
        assert original_releases == digest(root / 'owner_release_receipts.json')
        assert original_history == digest(root / 'goal_history.sqlite3')
        observed = reader.observe('captured-original')
        target, releases = observed.document, observed.releases
        assert observed.goal_history == GoalHistoryStore.acquire_read_only(root / 'registry.json')
        assert target.threads[captured.source.name] == captured.source
        assert releases['retired-original'].before == 1
        assert releases['retired-original'].after == 2
        process.stdin.write('advance-admission\n')
        process.stdin.flush()
        assert process.stdout.readline().strip() == 'advanced'
        try:
            captured.require_current()
        except subprocess.CalledProcessError:
            refused = True
        else:
            raise AssertionError('A changed admission reused original capture proof')
        receipt = {
            'complete': True, 'authentic_original_python': str(original_python),
            'original_registry_read_only': True, 'original_releases_read_only': True,
            'strict_target_registry': True, 'strict_target_release': True,
            'preserved_release_before_after': [1, 2],
            'distinct_original_arguments_retained': True, 'credentials_RAM_only': True,
            'changed_admission_refused': refused, 'provider_calls': 0,
            'native_inputs': 0, 'public_mutations': 0,
        }
        (stage / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt), flush=True)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
    assert process.returncode == 0, process.stderr.read()


if __name__ == '__main__':
    if os.environ.get('AC_REPLACEMENT_OUTPUT'):
        import asyncio
        asyncio.run(runtime_replacement())
    else:
        main()
