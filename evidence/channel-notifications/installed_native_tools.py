"""Actual installed native/tool path with loopback-only deterministic model output."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
# Add test-only libraries AFTER the installed runtime search paths.
sys.path.append(str(ROOT/'.venv/lib/python3.14/site-packages'))
sys.path.append(str(ROOT/'tests'))
import pytest
import agent_comms
from test_selected_execution_native import test_native_full_four_tools_publish_and_release
assert str(Path(agent_comms.__file__).resolve()).startswith('/home/ts/.local/share/agent-comms/runtime-acp-extensions-20260928/')
with tempfile.TemporaryDirectory(prefix='ac-installed-tools-',dir='/var/tmp') as directory:
 with pytest.MonkeyPatch.context() as changes:
  changes.setenv('AC_NATIVE_COPIED_PACKAGE','/var/tmp/agent-comms-pi-native-extensions-20260928/node_modules/@earendil-works/pi-coding-agent')
  asyncio.run(test_native_full_four_tools_publish_and_release(Path(directory),changes))
report={'verified':True,'core':agent_comms.__file__,'provider':'loopback fixture only; no paid requests','tools':['read','edit','write','bash'],'checks':'real native CLI, tool admission/effects, publication and release; fixtures removed'}
(HERE/'installed-native-tools.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
