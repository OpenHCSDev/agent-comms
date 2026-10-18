import asyncio, hashlib, json, os, shutil, subprocess, sys, time
from pathlib import Path
import psutil

from agent_comms.comms import Comms
from agent_comms.native_fork import ForkSessionHelper, ForkSessionRequest
from agent_comms.threads import Thread

EVIDENCE = Path('/home/ts/wt/core-doctrine-segment/.scratch/worker-memory')
ORIGINAL = Path('/home/ts/.pi/agent/sessions/--home-ts-.agent-comms--/2026-10-12T23-53-24-927Z_01a13289-0cc0-7155-a4ed-6142e4d26868.jsonl')
PACKAGE = Path('/home/ts/.local/share/agent-comms/native-current-84d389a90e2228d6/node_modules/@earendil-works/pi-coding-agent')
CHILD_SCRIPT = EVIDENCE / 'isolated_worker_stream_child.py'
LABEL = sys.argv[1]
DURATION = 30
STAGE = Path('/home/ts/.cache/agent-scratch') / f'doctrine-prefix-stream-{LABEL}'
RESULT = EVIDENCE / f'worker-prefix-stream-{LABEL}.json'
CHILD_SUMMARY = STAGE / 'child-summary.json'
LOG = STAGE / 'child.log'

async def prepare():
    STAGE.mkdir(mode=0o700, exist_ok=False)
    (STAGE / 'OWNER').write_text('owner=doctrine-segment purpose=isolated controlled streamed native-session worker CPU measurement\n')
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
        'memory-stream-worker', frozenset({'memory-measurement'}), str(project),
        task='Isolated controlled stream CPU measurement; no provider request.',
        session_file=fork.session_file, model='openai-codex/gpt-5.6-sol',
    ))
    return service, root_id, Path(fork.session_file), before

async def main():
    service, root_id, fork, original_hash = await prepare()
    fork_start = fork.stat().st_size
    env = dict(os.environ,
        AGENT_COMMS_ROOT=str(service.root), AGENT_COMMS_THREAD='memory-stream-worker',
        AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
        AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(PACKAGE),
        AGENT_COMMS_AGENT_BIN=str(Path(sys.executable).with_name('pi-comms-native')),
        AGENT_COMMS_RUNTIME_ROOT=str(Path(sys.executable).parent),
        MEMORY_FORK_SESSION=str(fork), MEMORY_STREAM_SUMMARY=str(CHILD_SUMMARY),
        MEMORY_STREAM_SECONDS=str(DURATION), PI_AGENT_ID='memory-stream-worker',
        PI_WORKTREE=str(STAGE / 'project'))
    for key in ('PI_PROMPT','PI_PARENT_ID','AGENT_COMMS_STARTUP_INPUT_KEY','PYTHONPATH'):
        env.pop(key, None)
    with LOG.open('w') as log:
        child = subprocess.Popen([sys.executable, str(CHILD_SCRIPT)], env=env,
                                 stdout=log, stderr=subprocess.STDOUT)
    process = psutil.Process(child.pid)
    begun = time.monotonic(); initial_cpu = process.cpu_times(); samples=[]
    try:
        while child.poll() is None:
            try:
                mem = process.memory_full_info(); cpu = process.cpu_times()
                samples.append({'elapsed_seconds':time.monotonic()-begun,
                                'cpu_seconds':cpu.user+cpu.system,
                                'rss_bytes':mem.rss,'uss_bytes':mem.uss})
            except psutil.NoSuchProcess:
                break
            await asyncio.sleep(0.5)
        code = child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.terminate(); child.wait(timeout=10)
    if code != 0 or not CHILD_SUMMARY.exists():
        raise RuntimeError(f'worker failed {code}: {LOG.read_text()}')
    summary=json.loads(CHILD_SUMMARY.read_text())
    elapsed=samples[-1]['elapsed_seconds'] if samples else time.monotonic()-begun
    cpu_delta=samples[-1]['cpu_seconds']-(initial_cpu.user+initial_cpu.system) if samples else summary['workload_cpu_seconds']
    result={'label':LABEL,'runtime_python':sys.executable,'pid':child.pid,
            'original':str(ORIGINAL),'original_bytes':ORIGINAL.stat().st_size,
            'original_sha256':original_hash,'original_unchanged':hashlib.sha256(ORIGINAL.read_bytes()).hexdigest()==original_hash,
            'fork_start_bytes':fork_start,'elapsed_seconds':elapsed,'cpu_seconds':cpu_delta,
            'cpu_percent_one_core':100*cpu_delta/elapsed,'rss_max':max(x['rss_bytes'] for x in samples),
            'uss_max':max(x['uss_bytes'] for x in samples),'samples':samples,'child':summary,'returncode':code}
    RESULT.write_text(json.dumps(result,indent=2)+'\n')
    shutil.rmtree(STAGE)
    print(RESULT); print(json.dumps({k:result[k] for k in ('elapsed_seconds','cpu_seconds','cpu_percent_one_core','rss_max','uss_max','child','returncode','original_unchanged')}))

if __name__ == '__main__': asyncio.run(main())
