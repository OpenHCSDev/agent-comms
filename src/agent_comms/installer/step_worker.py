"""Run one carry step's operation with the staged stack's interpreter.

``python -m agent_comms.installer.step_worker NAME INPUT OPERATION RESULT``, with the
target tree's ``src`` and ``tools`` on the path; writes the encoded result to ``RESULT``.
"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.carry import STEPS, Carry, CarryOperation


def main() -> None:
    name, encoded_step, encoded_operation, result_path = sys.argv[1:]
    import_module(f"{STEPS.name}.{name}")
    step = FieldCodec.decode(Carry.declared[name], FieldCodec.parse(encoded_step))
    operation = FieldCodec.decode(CarryOperation, FieldCodec.parse(encoded_operation))
    result = operation.run(step)
    Path(result_path).write_text(operation.encode_answer(result))


if __name__ == "__main__":
    main()
