"""Reuse the original configured SDK/ACP/commit journey; observe exact admission.

One functional private fork, original route/provider, no comparative experiment.
The original installer captures live launch custody; credentials remain in RAM.
"""
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import patch

from original_owner_capture import CurrentTypedCapture
from compaction_source_successor_installed_journey import run
from agent_comms.native_pi import NativePiRpcLaunch


@contextmanager
def observe_native_requests(package, observation, *, contexts=None, summaries=None, condition_source=None):
    """Borrow original native frames and retire every owned inspector on exit."""
    observer = Path(__file__).with_name('summary_prefix_native_observer.mjs').resolve()
    observers = []

    def observe_launch(environment):
        environment['AC_PREFIX_PACKAGE'] = str(package)
        environment['AC_PREFIX_OBSERVATION'] = str(observation)

    # The original launcher strips NODE_OPTIONS and fences imports. Observe via
    # Node's loopback debugger, leaving both policies and all product code intact.
    bootstrap = NativePiRpcLaunch.bootstrap

    def observed_bootstrap(cls, cli, arguments, cwd, environment, configuration):
        argv, environment = bootstrap(cli, arguments, cwd, environment, configuration)
        if environment.get('AC_PREFIX_OBSERVATION'):
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            observers.append(subprocess.Popen(
                ['node', str(observer), str(port), str(package), str(observation),
                 str(contexts) if contexts is not None else '',
                 str(summaries) if summaries is not None else '',
                 str(condition_source) if condition_source is not None else ''],
                env={'PATH': os.defpath}, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL))
            argv = (argv[0], f'--inspect-brk=127.0.0.1:{port}', *argv[1:])
        return argv, environment

    try:
        with patch.object(NativePiRpcLaunch, 'bootstrap', classmethod(observed_bootstrap)):
            yield observe_launch
    finally:
        for process in observers:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


async def main(stage, package, original_python):
    captured = CurrentTypedCapture(
        Path('/var/tmp/agent-comms-live-20260927-wzjtqhza'), original_python,
    ).read('openhcs-architecture-memory')
    source = captured.require_current()
    original_file = Path(source.require_saved_session())
    with original_file.open('rb') as stream:
        before = hashlib.file_digest(stream, 'sha256').hexdigest()
    observation = stage / 'prefix-observation.jsonl'
    def capture_source():
        return captured.require_current(), captured.retained
    try:
        with observe_native_requests(package, observation) as observe_launch:
            await run(stage, package, original_file,
                      capture_source=capture_source, observe_launch=observe_launch,
                      probe_marker=f'SOURCE527_{stage.name.upper().replace("-", "_")}_AFTER_COMMIT')
        records = [json.loads(line) for line in observation.read_text().splitlines()]
        assert not any(r.get('observerFailed') or r.get('unqualifiedRouteCancelledBeforeRequest') for r in records)
        routes = [r for r in records if r.get('stage') == 'route']
        requests = [r for r in records if r.get('stage') == 'selected-request']
        assert len(routes) == len(requests) == 1, 'Expected one admitted original-prefix summary, not bounded/map execution'
        route, request = routes[0], requests[0]
        assert route['canonicalEndpoint'] and request['canonicalEndpoint']
        assert request['toolChoiceNone'] and request['originalInstructions'] and request['originalTools'] and request['boundProvider']
        for key in ('provider', 'api', 'systemHash', 'toolsHash', 'orderedPrefixHash', 'messageCount', 'toolCount'):
            assert route[key] == request[key], key
        assert request['messageCount'] > 0 and request['toolCount'] > 0
        with original_file.open('rb') as stream:
            unchanged = before == hashlib.file_digest(stream, 'sha256').hexdigest()
        result = {'complete': True, 'actual_prefix_admitted': True, 'bounded_fallback_qualifies': False,
                  'source_unchanged': unchanged,
                  'package': str(package), 'original_route': route, 'selected_request': request,
                  'body_or_credentials_exported': False, 'paid_comparison': False,
                  'observation_scope': 'Original native API formation and selectedStream before auth; actual configured SDK/ACP provider completion and journal commit are in receipt.json'}
        assert result['source_unchanged']
        (stage / 'prefix-functional-receipt.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)
    except BaseException as error:
        if stage.exists():
            (stage / 'prefix-functional-failure.json').write_text(json.dumps({
                'complete': False, 'type':type(error).__name__, 'reason':str(error),
                'original_preserved':True, 'retry_or_replay':False,
            }, indent=2) + '\n')
        raise


if __name__ == '__main__':
    # A venv interpreter symlink is a launch capability. Resolving it to the
    # shared UV executable discards the original installed package environment.
    asyncio.run(main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), Path(sys.argv[3]).absolute()))
