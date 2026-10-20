"""Run one carry step's operation with the staged stack's interpreter.

``python -m agent_comms.installer.step_worker NAME METHOD ROOT ARGUMENTS``, with the
target tree's ``src`` and ``tools`` on the path; prints the encoded result.
"""

from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path
from typing import get_type_hints

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.carry import STEPS, Carry


def main() -> None:
    name, method, root, encoded_arguments = sys.argv[1:]
    import_module(f"{STEPS.name}.{name}")
    step = Carry.declared[name]
    annotation = get_type_hints(getattr(Carry, method), include_extras=True)["return"]
    arguments = FieldCodec.decode(dict[str, bool], FieldCodec.parse(encoded_arguments))
    result = getattr(step, method)(Path(root), **arguments)
    print(json.dumps(FieldCodec.encode(result, annotation)))


if __name__ == "__main__":
    main()
