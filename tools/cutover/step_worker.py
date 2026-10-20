"""Invoke a declared carry operation under its selected source and interpreter."""

from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path
from typing import Annotated, get_type_hints

from agent_comms.field_codec import FieldCodec, PathText

from .carry import Carry


def main() -> None:
    module, declaration, method, root, encoded_arguments = sys.argv[1:]
    # Historical declarations name Path without its wire representation. Resolve
    # that annotation through the existing codec, not a second path serializer.
    annotation = get_type_hints(
        getattr(Carry, method),
        localns={"Path": Annotated[Path, PathText]},
        include_extras=True,
    )["return"]
    arguments = FieldCodec.decode(dict[str, bool], FieldCodec.parse(encoded_arguments))
    step = getattr(import_module(module), declaration)
    result = getattr(step, method)(Path(root), **arguments)
    print(json.dumps(FieldCodec.encode(result, annotation)))


if __name__ == "__main__":
    main()
