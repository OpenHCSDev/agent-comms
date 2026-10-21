"""Run one carry step's operation with the staged stack's interpreter.

``python -m agent_comms.installer.step_worker NAME METHOD REQUEST``, with the
target tree's ``src`` and ``tools`` on the path; prints the encoded result.
"""

from __future__ import annotations

import inspect
import json
import sys
from importlib import import_module

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.carry import STEPS, Carry


def main() -> None:
    name, method, encoded_request = sys.argv[1:]
    import_module(f"{STEPS.name}.{name}")
    step = FieldCodec.decode(Carry.declared[name], FieldCodec.parse(encoded_request))
    annotation = inspect.signature(getattr(Carry, method), eval_str=True).return_annotation
    result = getattr(step, method)()
    print(json.dumps(FieldCodec.encode(result, annotation)))


if __name__ == "__main__":
    main()
