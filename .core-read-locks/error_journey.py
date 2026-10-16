"""Real SQLite and CLI watch: busy waits, broken data fails; no provider calls."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import sqlite3
import subprocess
import sys
from agent_comms.cli_protocol import CORE_ERROR, FailedCoreError, UnavailableCoreError
from agent_comms.comms import Comms
from agent_comms.coordination_database import CoordinationStore, StoreBusy
from agent_comms.coordination_errors import CoordinationReadBusy, CoordinationReadFailed
from agent_comms.threads import Thread

scratch=Path('/home/ts/.cache/agent-scratch/core-read-locks')
with TemporaryDirectory(prefix='error-api-',dir=scratch) as folder:
    root=Path(folder)
    probe=root/'probe.sqlite3'
    with closing(sqlite3.connect(probe)) as writer:
        writer.execute('CREATE TABLE value (n INTEGER)')
        writer.commit()
        writer.execute('BEGIN EXCLUSIVE')
        try:
            with CoordinationStore.observing(probe,StoreBusy()):pass
        except CoordinationReadBusy as error:
            assert error.watch_delay()==0.25 and error.__cause__.sqlite_errorcode==sqlite3.SQLITE_BUSY
        else:raise AssertionError('Busy engine read was not reported as busy')
        writer.rollback()
    try:
        with CoordinationStore.observing(root/'missing.sqlite3',StoreBusy()):pass
    except CoordinationReadFailed as error:
        try:error.watch_delay()
        except CoordinationReadFailed as caught:assert caught is error
        else:raise AssertionError('A failed read asked for another attempt')
    else:raise AssertionError('Missing database did not fail')
    comms=Comms(root/'wire')
    comms.registry.declare(Thread('sender',frozenset(),str(root)))
    comms.messaging.initialize_private_initial_protocol()
    viewer=comms.messaging.user_identity(str(root)).name
    message=comms.messaging.send_message('sender',viewer,'new isolated read')
    assert comms.bus.pending_count(viewer)==1
    (scratch/'error-api-effects.json').write_text(json.dumps({'message':message.seq,'root':str(comms.root)}))
    environment={k:v for k,v in os.environ.items() if not k.startswith(('AGENT_COMMS_','PI_'))}
    environment['PYTHONPATH']=str(Path.cwd()/'src')
    command=(sys.executable,'-m','agent_comms.cli','--root',str(comms.root),'watch','--thread',viewer)
    def watch():
        result=subprocess.run(command,env=environment,text=True,capture_output=True,timeout=20)
        assert result.returncode==1,(result.returncode,result.stdout,result.stderr)
        assert len(result.stdout.splitlines())==1,result.stdout
        return CORE_ERROR.validate_json(result.stdout)
    with closing(sqlite3.connect(comms.bus.reads.path)) as writer:
        writer.execute('BEGIN EXCLUSIVE')
        busy=watch()
        assert isinstance(busy,UnavailableCoreError),busy
        assert busy.watch_delay()==0.25
        writer.rollback()
    comms.bus.reads.path.write_bytes(b'not a SQLite database')
    failed=watch()
    assert isinstance(failed,FailedCoreError),failed
    try:failed.watch_delay()
    except RuntimeError:pass
    else:raise AssertionError('Failed CLI read was treated as busy')
    result={'sqlite_busy':'CoordinationReadBusy','sqlite_failed':'CoordinationReadFailed',
            'actual_cli_busy':busy.wire_tag,'actual_cli_failed':failed.wire_tag}
    (scratch/'error-api-result.json').write_text(json.dumps(result))
    print(json.dumps(result))
