import asyncio, hashlib, json, os, shutil, signal, subprocess, sys, time
from pathlib import Path
import psutil

from agent_comms.comms import Comms
from agent_comms.native_fork import ForkSessionHelper, ForkSessionRequest
from agent_comms.threads import Thread

EVIDENCE = Path('/home/ts/wt/core-doctrine-segment/.scratch/worker-memory')
STAGE = EVIDENCE / 'isolated-worker-stage-baseline'
ORIGINAL = Path('/home/ts/.pi/agent/sessions/--home-ts-wt-toad-close-lifecycle--/2026-10-13T07-12-27-696Z_01a1341b-0231-7179-9990-6fb9b9ffe526.jsonl')
PACKAGE = Path('/home/ts/.local/share/agent-comms/native-current-6152254fee45b5ed/node_modules/@earendil-works/pi-coding-agent')
CHILD_SCRIPT = EVIDENCE / 'isolated_worker_child.py'
RESULT = EVIDENCE / 'worker-20min-baseline.json'
SAMPLE_SECONDS = 30
DURATION_SECONDS = 1200

async def prepare():
    STAGE.mkdir(mode=0o700, exist_ok=False)
    project = STAGE / 'project'; project.mkdir(mode=0o700)
    before = hashlib.sha256(ORIGINAL.read_bytes()).hexdigest()
    fork = await ForkSessionHelper.run(ForkSessionRequest(
        str(PACKAGE), str(ORIGINAL), str(project), str(STAGE / 'forks')
    ), cwd=project, env=dict(os.environ))
    assert hashlib.sha256(ORIGINAL.read_bytes()).hexdigest() == before
    service = Comms(STAGE / 'wire', private_initial_writes=True)
    root_id = service.messaging.initialize_private_initial_protocol()
    service.owners.pin_private_nk_launch(service.root, root_id, PACKAGE)
    service.registry.declare(Thread(
        'memory-fork-worker', frozenset({'memory-measurement'}), str(project),
        task='Isolated saved-session memory measurement; no input or provider request.',
        session_file=fork.session_file, model='openai-codex/gpt-5.6-sol',
    ))
    return service, root_id, Path(fork.session_file), before

def status(process):
    memory = process.memory_full_info()
    return {'elapsed_seconds': round(time.monotonic() - begun, 3),
            'rss_bytes': memory.rss, 'uss_bytes': memory.uss,
            'threads': process.num_threads()}

async def main():
    global begun
    service, root_id, fork, original_hash = await prepare()
    env = dict(os.environ,
        AGENT_COMMS_ROOT=str(service.root), AGENT_COMMS_THREAD='memory-fork-worker',
        AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
        AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(PACKAGE),
        AGENT_COMMS_AGENT_BIN=str(Path(sys.executable).with_name('pi-comms-native')),
        AGENT_COMMS_RUNTIME_ROOT=str(Path(sys.executable).parent),
        MEMORY_FORK_SESSION=str(fork), PI_AGENT_ID='memory-fork-worker',
        PI_WORKTREE=str(STAGE/'project'))
    for key in ('PI_PROMPT','PI_PARENT_ID','AGENT_COMMS_STARTUP_INPUT_KEY','PYTHONPATH'):
        env.pop(key, None)
    log = (EVIDENCE / 'isolated-worker-baseline.log').open('w')
    child = subprocess.Popen([sys.executable, str(CHILD_SCRIPT)], env=env,
                             stdout=log, stderr=subprocess.STDOUT)
    process = psutil.Process(child.pid)
    begun = time.monotonic()
    result = {'runtime_python':sys.executable,'pid':child.pid,'sample_seconds':SAMPLE_SECONDS,
              'duration_seconds':DURATION_SECONDS,'original':str(ORIGINAL),
              'original_bytes':ORIGINAL.stat().st_size,'original_sha256':original_hash,
              'fork':str(fork),'samples':[]}
    try:
        for index in range(DURATION_SECONDS // SAMPLE_SECONDS + 1):
            if child.poll() is not None:
                raise RuntimeError(f'isolated worker exited {child.returncode}')
            result['samples'].append(status(process))
            RESULT.write_text(json.dumps(result,indent=2)+'\n')
            if index < DURATION_SECONDS // SAMPLE_SECONDS:
                await asyncio.sleep(SAMPLE_SECONDS)
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try: child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()
        log.close()
        result['returncode'] = child.returncode
        result['original_unchanged'] = hashlib.sha256(ORIGINAL.read_bytes()).hexdigest() == original_hash
        RESULT.write_text(json.dumps(result,indent=2)+'\n')
        shutil.rmtree(STAGE)
    print(RESULT)
    print(json.dumps({'samples':len(result['samples']),
        'rss_min':min(x['rss_bytes'] for x in result['samples']),
        'rss_max':max(x['rss_bytes'] for x in result['samples']),
        'rss_last':result['samples'][-1]['rss_bytes'],
        'uss_max':max(x['uss_bytes'] for x in result['samples']),
        'returncode':result['returncode'],'original_unchanged':result['original_unchanged']}))

asyncio.run(main())
