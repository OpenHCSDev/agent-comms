from pathlib import Path
import importlib.util, json, os, select, sys, time, subprocess
core = Path('/home/ts/wt/comms-external-cli-participation-20261001')
toad = Path('/home/ts/wt/toad-external-cli-conversation-20261001')
base = core / '.artifacts/external-cli06'
runtime = core / '.artifacts/runtime-external-cli-candidate'
spec = importlib.util.spec_from_file_location('recorder', toad/'tests/tools/record_installed_tui.py')
module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
owner = module.ProcessOwner()
env = os.environ.copy()
for key in ('PYTHONPATH','DISPLAY','WAYLAND_DISPLAY','NO_COLOR','AGENT_COMMS_ROOT','AGENT_COMMS_THREAD','AGENT_COMMS_MANAGED','PI_AGENT_ID','PI_PROMPT','PI_PARENT_ID','AGENT_COMMS_RUNTIME_ROOT'):
    env.pop(key, None)
package = '/home/ts/wt/comms-retained-task-facts-s2-20261001/stack/.pi-native-0064a96bb79c21c1/node_modules/@earendil-works/pi-coding-agent'
env.update(AC_NATIVE_COPIED_PACKAGE=package,L0A_EVIDENCE=str(base/'proof'),TMPDIR=str(base),EXTERNAL_CLI_FIXTURE='/home/ts/wt/k487a06',TOAD_TEST_ATTEMPT='kepler-external-cli06',PATH=str(runtime/'bin')+os.pathsep+env['PATH'])
receipt={'started':time.time(),'runtime':str(runtime),'native':package,'source_entrypoint':str(toad/'tests/external_cli_native_installed_pilot.py')}
reader, writer = os.pipe()
try:
    with (base/'launch.log').open('wb') as log:
        xvfb=owner.start(['Xvfb','-displayfd',str(writer),'-screen','0','1700x1100x24','-nolisten','tcp'],pass_fds=(writer,),stdout=log,stderr=log)
        os.close(writer)
        if not select.select([reader],[],[],10)[0]: raise TimeoutError('Private Xvfb did not publish display')
        env['DISPLAY']=':'+os.read(reader,32).decode().strip()
        os.close(reader)
        assert env['DISPLAY']!=':0'
        receipt['display']=env['DISPLAY']
        terminal=owner.start(['st','-g','160x44+0+0','-e',str(runtime/'bin/python'),receipt['source_entrypoint']],env=env,cwd=str(toad),stdout=log,stderr=log)
        terminal.process.wait(timeout=90)
        receipt['terminal_exit']=terminal.process.returncode
        receipt['passed']=(base/'proof/acceptance-complete.txt').exists()
        if not receipt['passed']: raise RuntimeError('Continuous external CLI acceptance did not complete; original proof retained')
finally:
    receipt['cleanup']=owner.cleanup()
    receipt['finished']=time.time()
    (base/'capture-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt))
