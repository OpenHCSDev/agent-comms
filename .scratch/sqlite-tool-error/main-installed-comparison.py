"""Fresh isolated main comparison; preserve the earlier failed candidate unchanged."""
import importlib.util
import json
import sys
from pathlib import Path
import pytest

package = Path('/home/ts/.local/share/agent-comms/runtimes/toad-bce836bde-core-00e34175b-native-6468a1979d983e9d-20261016T023112Z/lib/python3.14/site-packages/agent_comms')
spec = importlib.util.spec_from_file_location('agent_comms', package / '__init__.py', submodule_search_locations=[str(package)])
agent_comms = importlib.util.module_from_spec(spec)
sys.modules['agent_comms'] = agent_comms
spec.loader.exec_module(agent_comms)
from compaction_loopback import LoopbackProvider

root = Path('/home/ts/wt/core-doctrine-segment')
installed = Path(agent_comms.__file__).resolve()
assert 'core-00e34175b-' in str(installed), installed
print('MAIN_INSTALLED', installed, flush=True)
handle = LoopbackProvider.handle
providers = []
async def retain_requests(self, *args):
    providers.append(self)
    return await handle(self, *args)
LoopbackProvider.handle = retain_requests
result = pytest.main([
    '-vv', '-s', '-x', '-n', '0', '--no-cov', '--tb=short',
    '--basetemp='+str(root/'.scratch/sqlite-tool-error/main-installed-family'),
    'tests/test_acp_input_disposition.py::test_waiting_messages_and_a_model_change_reach_the_next_model_call_together',
])
if providers:
    (root / '.scratch/sqlite-tool-error/main-model-change-requests.json').write_text(
        json.dumps(providers[-1].requests, indent=2)
    )
raise SystemExit(result)
