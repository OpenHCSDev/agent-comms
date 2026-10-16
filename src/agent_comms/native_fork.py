"""Selected-installation entrypoint; fork declarations keep their canonical module."""

from __future__ import annotations

import asyncio
import json
import sys

from .field_codec import FieldCodec
from .native_session_fork import RuntimeForkHelper


def main() -> int:
    request = FieldCodec.decode(RuntimeForkHelper.request, json.load(sys.stdin))
    result = asyncio.run(request.copy())
    print(json.dumps(FieldCodec.encode(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
