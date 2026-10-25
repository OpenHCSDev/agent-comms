"""The selected runtime reads its own CLI result, then emits an installer report.

OwnerActionsProbe supplies this module's source to an isolated interpreter. Do
not import the installing Core here: both CLI execution and batch decoding must
use the selected runtime, whose result shape may differ from the installing one.
"""

from __future__ import annotations

import sys
from io import StringIO

from agent_comms.cli import _fail, execute
from agent_comms.cli_commands import TargetBatchResult
from agent_comms.cli_protocol import CLI_FAILURES
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.owner_starts import OwnerStart


def main(arguments: list[str]) -> int:
    output = StringIO()
    execute(arguments, output)
    batch = FieldCodec.decode(TargetBatchResult, FieldCodec.parse(output.getvalue()))
    owners = tuple(outcome.owner_start() for outcome in batch.outcomes)
    print(FieldCodec.compact(FieldCodec.encode(owners, tuple[OwnerStart, ...])))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except CLI_FAILURES as error:
        raise SystemExit(_fail(error))
