"""Fresh Core read journey; no provider, original messages or live store writes."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
from agent_comms.comms import Comms
from agent_comms.threads import Thread
from agent_comms.read_ledger import OneViewer

scratch=Path('/home/ts/.cache/agent-scratch/core-read-locks')
with TemporaryDirectory(prefix='read-api-',dir=scratch) as folder:
    root=Path(folder)
    comms=Comms(root)
    for name in ('sender','noise'):
        comms.registry.declare(Thread(name,frozenset(),str(root)))
    comms.messaging.initialize_private_initial_protocol()
    viewer=comms.messaging.user_identity(str(root)).name
    first=comms.messaging.send_message('sender',viewer,'first')
    comms.messaging.send_message('noise','#other','not this conversation')
    middle=comms.messaging.send_message('sender',viewer,'middle')
    last=comms.messaging.send_message('sender',viewer,'last')
    (scratch/'read-api-effects.json').write_text(json.dumps({'root':str(root),'sent':[first.seq,middle.seq,last.seq]}))
    snapshot=comms.registry.snapshot()
    counts=[]
    for message in (last,first,middle):
        displayed=comms.bus.reads.capture(viewer,(message,),snapshot,comms.bus.log.path)
        comms.bus.reads.mark_displayed(viewer,displayed,snapshot=snapshot)
        counts.append(comms.bus.pending_count(viewer,'sender'))
    assert counts==[2,1,0],counts
    state,=comms.bus.reads.read(OneViewer(snapshot.participant(viewer).incarnation)).messages.values()
    assert state.read_through==last.seq and not state.sparse
    result={'pending':counts,'last_read':state.read_through,'messages':last.seq}
    (scratch/'read-api-result.json').write_text(json.dumps(result))
    print(json.dumps(result))
