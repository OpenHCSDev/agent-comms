import asyncio, os, subprocess, sys
from pathlib import Path
from support.roots import Roots
from support.processes import stop_test_children
root=Path('/home/ts/wt/p107ui-v2')
assert not root.exists(), 'Never replay a retained UI attempt'
root.mkdir()
env=Roots(root).environment(dict(os.environ))
env['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'
try:
    with Path('/home/ts/wt/core-p107/.artifacts/p107/ui-check-v2.txt').open('w') as log:
        result=subprocess.run([sys.executable,'-m','pytest','-o','addopts=','-q',
            'tests/textual/test_goal_owner_views.py::test_declared_progress_updates_bar_editor_and_details_without_presence_probes'],
            cwd='/home/ts/wt/toad-p107',env=env,stdout=log,stderr=subprocess.STDOUT,timeout=90)
    print('UI exit',result.returncode,'root',root)
finally:
    asyncio.run(stop_test_children(str(root)))
