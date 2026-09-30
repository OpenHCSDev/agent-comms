"""Create genuine old720 annotations with its public APIs and real Pi SDK."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.input_disposition import InputDispositions
from agent_comms.messages import Message
from agent_comms.routing import MessageRoute, TurnRouting


def main():
    root, package = map(Path, sys.argv[1:])
    seeded = subprocess.run([sys.executable, str(Path(__file__).with_name('seed_retained_index_fixture.py')),
                             str(root)], check=True, capture_output=True, text=True)
    seed = json.loads(seeded.stdout)
    service = Comms(root)
    original = Message.from_committed_wire(seed['original'])
    prompt = 'Original exact native request'
    script = """
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
const {SessionManager} = await import(pathToFileURL(join(process.argv[1],
  'dist/core/session-manager.js')));
const manager = SessionManager.create(process.argv[2], join(process.argv[2], 'sessions'));
const user = manager.appendMessage({role:'user',content:process.argv[3],
  inputId:'a'.repeat(32), inputDigest:process.argv[4],timestamp:Date.now()});
const final = manager.appendMessage({role:'assistant',content:[{type:'text',text:'Retained native answer'}],
  provider:'fixture',model:'fixture',api:'openai-completions',stopReason:'stop',timestamp:Date.now()});
console.log(JSON.stringify({session:manager.getSessionFile(),user,final}));
"""
    native = json.loads(subprocess.run(['node', '--input-type=module', '-e', script,
        str(package), str(root.parent), prompt, hashlib.sha256(prompt.encode()).hexdigest()],
        check=True, capture_output=True, text=True).stdout)
    path = Path(native['session'])
    os.chmod(path, 0o600)
    service.registry.register(replace(service.registry.require('alpha'),
        process_identity=ProcessIdentity.capture(os.getpid())))
    service.threads.attach_session('alpha', str(path))
    routing = TurnRouting((original,), MessageRoute('alpha', ('#team',)))
    service.transcripts.routes.record(str(path), (native['user'], native['final']), routing)
    service.transcripts.routes.record_input_display('a' * 32, 'Retained displayed input',
        sent_text=prompt, routing=TurnRouting((original,), None))
    service.transcripts.routes.record_input_display('b' * 32, None, sent_text='Retained internal input')
    inputs = InputDispositions(root / InputDispositions.filename)
    assert inputs.record('acp:unknown-original', seq=None, owner='alpha', admission=1,
                         target='alpha', text='Protected UNKNOWN original')
    assert inputs.bind('acp:unknown-original', admission=1, turn_id='retained-unknown',
                       native_id='c' * 32, text='Protected UNKNOWN native delivery')
    # A genuine expired original with reply-only routing is still carried;
    # no turn is executed and the original process exits before the batch.
    service.agents.begin_turn('alpha', 'retained-original-turn', 'Original no-provider custody',
                             TurnRouting((), MessageRoute('alpha', ('#team',))))
    print(json.dumps({**seed, **native}), flush=True)


if __name__ == '__main__':
    main()
