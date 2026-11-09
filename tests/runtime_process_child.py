"""The child the old-runtime parent starts: it prints the runtime tree it observes."""

import sys

from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.runtime_processes import RuntimeProcesses
from agent_comms.nominal.field_codec import FieldCodec

if __name__ == "__main__":
    print(FieldCodec.compact(FieldCodec.encode(RuntimeProcesses.observe(
        LaunchRuntime.from_interpreter(sys.argv[1])))))
