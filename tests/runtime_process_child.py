"""The child the old-runtime parent starts: it prints the runtime tree it observes."""

import sys

from agent_comms.field_codec import FieldCodec
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.runtime_processes import RuntimeProcesses

if __name__ == "__main__":
    print(FieldCodec.compact(FieldCodec.encode(RuntimeProcesses.observe(
        LaunchRuntime.from_interpreter(sys.argv[1])))))
