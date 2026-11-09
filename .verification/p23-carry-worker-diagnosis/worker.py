"""Normal-user worker launch with a retained Python stack if read-back stalls."""
from pathlib import Path
import faulthandler

stack = Path(__file__).with_name('worker-stack.log').open('w')
faulthandler.dump_traceback_later(8, file=stack, repeat=False)

from agent_comms.installer.interpreter_operation import serve

try:
    serve()
finally:
    faulthandler.cancel_dump_traceback_later()
    stack.close()
