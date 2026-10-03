"""Observe real package/header calls in the existing installed owner journey.

Only stdlib thread profiling records code entry/exit; the original operations,
state, protocol and decisions execute unchanged. No prompt/provider is sent.
"""
import json
import runpy
import sys
import threading
import time
from pathlib import Path

from agent_comms.native_package import verify_native_package
from agent_comms.native_session_reopen import SessionIdentityHelper

output = Path(__file__).with_name('package-profile.json')
code = {verify_native_package.__code__: 'verify_native_package',
        SessionIdentityHelper.locate.__func__.__code__: 'SessionIdentityHelper.locate'}
events = []

def observe(frame, event, arg):
    if frame.f_code in code and event in ('call', 'return'):
        events.append({'operation': code[frame.f_code], 'event': event,
                       'monotonic_ns': time.monotonic_ns(), 'thread': threading.get_ident()})

original = threading.getprofile()
try:
    threading.setprofile_all_threads(observe)
    sys.argv[0] = 'evidence/native-operation-custody-20261002/installed-context-owner.py'
    runpy.run_path(sys.argv[0], run_name='__main__')
finally:
    threading.setprofile_all_threads(original)
    output.write_text(json.dumps({'scope': 'bounded profiling of original package/header functions, not provider or complete latency attribution',
                                 'clock': 'same process monotonic_ns including worker threads',
                                 'events': events}, indent=2) + '\n')
