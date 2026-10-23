"""Run one carry operation over a pass of steps with the staged stack's interpreter.

``python -m agent_comms.installer.step_worker OPERATION RESULT STEPS``, with the target
tree's ``src`` and ``tools`` on the path. ``STEPS`` lists each step's declared name and
encoded input in pass order; one process answers the whole pass, so a pass pays the
interpreter's start once, not once per step. Writes the encoded answers to ``RESULT``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.carry import Carry, CarryOperation


def main() -> None:
    encoded_operation, result_path, encoded_steps = sys.argv[1:]
    operation = FieldCodec.decode(CarryOperation, FieldCodec.parse(encoded_operation))
    steps = []
    for name, encoded_step in FieldCodec.parse(encoded_steps):
        steps.append(FieldCodec.decode(Carry.member_named(name), encoded_step))
    Path(result_path).write_text(operation.encode_answers(
        tuple(operation.run(step) for step in steps)))


if __name__ == "__main__":
    main()
