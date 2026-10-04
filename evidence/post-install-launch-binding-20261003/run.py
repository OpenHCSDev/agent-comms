from pathlib import Path
import os,subprocess,sys
here=Path(__file__).parent; root=here/'private-root'; assert not root.exists()
original=Path('/home/ts/wt/comms-task-aware-native-bundle-20261002/.artifacts/runtime-summary-generation-policy520-installed-20261002/bin/python')
target=Path('/home/ts/wt/toad-s5-receiving-boundary-pair-20261001/.artifacts/runtime-historical-handling290-current-20261001/bin/python')
package=Path('/home/ts/wt/comms-task-aware-native-bundle-20261002/stack/.pi-native-960296fdddafb01c/node_modules/@earendil-works/pi-coding-agent')
env=dict(os.environ);env.pop('PYTHONPATH',None)
subprocess.run([str(original),str(here/'source.py'),str(root),str(package)],env=env,check=True)
subprocess.run([str(target),str(here/'transition.py'),str(root),str(package)],env=env,check=True)
