"""Run only the published same-input reader; use the existing child owner."""
from contextlib import ExitStack
from pathlib import Path
import json
import os
import subprocess
import sys
import time

from agent_comms.child_process import ParentedProcess
from agent_comms.field_codec import FieldCodec
from publish_retained_summary import ReviewedArtifact

command_path = Path(sys.argv[1])
ReviewedArtifact(command_path, sys.argv[2]).require_original()
command = json.loads(command_path.read_text())
environment = dict(os.environ)
environment.update(command["environment"])
output = Path(command["output"])
started = time.monotonic()
identity = None
child = None
with ExitStack() as resources:
    child = resources.enter_context(ParentedProcess.launch(
        tuple(command["command"]), cwd=command["cwd"], env=environment,
        output=subprocess.PIPE,
    ))
    identity = child.identity
    stdout, stderr = child.process.communicate()
    exit_code = child.process.returncode
# The context owner joins retirement/reap and closes both captured pipe handles.
output.mkdir(mode=0o700, exist_ok=True)
for name, raw in (("stdout.log", stdout), ("stderr.log", stderr)):
    path = output / name
    with path.open("xb") as stream:
        stream.write(raw)
    path.chmod(0o600)
terminal = output / "command-terminal.json"
with terminal.open("x") as stream:
    json.dump(FieldCodec.encode(dict(
        exit_code=exit_code, elapsed_seconds=time.monotonic()-started,
        identity=identity, joined=True, retired=child.retired,
        command_receipt=ReviewedArtifact(command_path, sys.argv[2]),
        new_inputs=0, provider_calls=0, SDK_processes=0,
        scope="One same-input recorded read; original ParentedProcess owns exact child/group retirement",
    )), stream, indent=2)
    stream.write("\n")
terminal.chmod(0o600)
print(json.dumps(dict(exit_code=exit_code, retired=child.retired,
                      output=str(output))))
raise SystemExit(exit_code)
