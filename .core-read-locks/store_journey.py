"""Real JSON-store I/O: unchanged reads, another writer and strict failure."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
from agent_comms.locked_store import JsonDocument
from agent_comms.shared_ledger import SharedLedger

scratch=Path('/home/ts/.cache/agent-scratch/core-read-locks')
with TemporaryDirectory(prefix='store-api-',dir=scratch) as folder:
    path=Path(folder)/'ledger.json'
    store=SharedLedger(path)
    store.merge({'value':1},'first')
    original=JsonDocument._decode
    decoded=[]
    def count(owner,data):
        decoded.append(data)
        return original(owner,data)
    with patch.object(SharedLedger,'_decode',count):
        first=store.read()
        assert all(store.read() is first for _ in range(20))
        assert len(decoded)==1
        SharedLedger(path).merge({'value':2},'second')
        after_other_writer=len(decoded)
        second=store.read()
        assert second['value']==2 and second is not first
        assert store.read() is second
        assert len(decoded)==after_other_writer+1
        path.write_text('{broken')
        for _ in range(2):
            try:store.read()
            except ValueError:pass
            else:raise AssertionError('Malformed bytes must raise, not reuse the old value')
    result={'unchanged_reads':21,'unchanged_decodes':1,'other_writer_seen':second['value'],'malformed_reads_raised':2}
    (scratch/'store-api-result.json').write_text(json.dumps(result))
    print(json.dumps(result))
