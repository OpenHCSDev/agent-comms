"""Reuse the original configured SDK/ACP/commit journey; observe exact admission.

One functional private fork, original route/provider, no comparative experiment.
The original installer captures live launch custody; credentials remain in RAM.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

from original_owner_capture import CurrentTypedCapture
from compaction_source_successor_installed_journey import run


async def main(stage, package, original_python):
    captured = CurrentTypedCapture(
        Path('/var/tmp/agent-comms-live-20260927-wzjtqhza'), original_python,
    ).read('openhcs-architecture-memory')
    source = captured.require_current()
    original_file = Path(source.require_saved_session())
    before = hashlib.file_digest(original_file.open('rb'), 'sha256').hexdigest()
    observation = stage / 'prefix-observation.jsonl'
    observer = Path(__file__).with_name('summary_prefix_native_observer.mjs').resolve()

    def capture_source():
        return captured.require_current(), captured.retained

    def observe_launch(environment):
        options = shlex.split(environment.get('NODE_OPTIONS', ''))
        environment['NODE_OPTIONS'] = shlex.join([*options, '--import', observer.as_uri()])
        environment['AC_PREFIX_PACKAGE'] = str(package)
        environment['AC_PREFIX_OBSERVATION'] = str(observation)

    try:
        await run(stage, package, original_file,
                  capture_source=capture_source, observe_launch=observe_launch)
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
        result = {'complete': True, 'actual_prefix_admitted': True, 'bounded_fallback_qualifies': False,
                  'source_unchanged': before == hashlib.file_digest(original_file.open('rb'), 'sha256').hexdigest(),
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
    asyncio.run(main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve()))
