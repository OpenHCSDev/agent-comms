"""Invoke a declared carry operation under its selected source and interpreter.

The installer copies this file into each step revision's ``tools/cutover`` and runs
it as ``cutover.step_worker``, so it imports nothing that revision may lack.
"""

from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path
from typing import Annotated, get_type_hints

from agent_comms.field_codec import FieldCodec, PathText


def main() -> None:
    module, declaration, method, root, encoded_arguments = sys.argv[1:]
    step = getattr(import_module(module), declaration)
    # Historical declarations name Path without its wire representation. Resolve
    # that annotation through the existing codec, not a second path serializer.
    annotation = get_type_hints(
        getattr(step, method),
        localns={"Path": Annotated[Path, PathText]},
        include_extras=True,
    )["return"]
    arguments = FieldCodec.decode(dict[str, bool], FieldCodec.parse(encoded_arguments))
    result = getattr(step, method)(Path(root), **arguments)
    print(json.dumps(FieldCodec.encode(result, annotation)))


if __name__ == "__main__":
    main()
