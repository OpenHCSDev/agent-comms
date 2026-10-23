"""Run one carry operation over a pass of steps with the staged stack's interpreter.

``python -m agent_comms.installer.step_worker REQUEST RESULT``, with the target
tree's ``src`` and ``tools`` on the path. ``REQUEST`` names the file containing the
operation and its typed steps in pass order; one process answers the whole pass,
so a pass pays the interpreter's start once, not once per step. Writes the encoded
answers to the file named by ``RESULT``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.carry import CarryRequest


def main() -> None:
    request_path, result_path = sys.argv[1:]
    request = FieldCodec.decode(CarryRequest, FieldCodec.parse(Path(request_path).read_text()))
    Path(result_path).write_text(request.run())


if __name__ == "__main__":
    main()
