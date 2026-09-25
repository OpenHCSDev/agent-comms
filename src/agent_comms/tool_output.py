"""Bound inline tool results while preserving complete public output in private files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .declarations import _atomic_write_text

MAX_INLINE_OUTPUT_BYTES = 32 * 1024


def materialize_oversized_output(
    root: Path, response: dict[str, object], *, inline_limit: int = MAX_INLINE_OUTPUT_BYTES
) -> Path | None:
    """Publish a generated public-result snapshot only when its transport is oversized.

    Pretty ASCII JSON conservatively bounds the Pi adapter's JSON.stringify text,
    which can emit literal Unicode. Files are read-only snapshots, never authority.
    """
    text = json.dumps(response, indent=2, ensure_ascii=True)
    encoded = text.encode("utf-8")
    if len(encoded) <= inline_limit:
        return None
    directory = root / "tool-output"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / f"{hashlib.sha256(encoded).hexdigest()}.json"
    if path.exists() and path.read_bytes() == encoded:
        path.chmod(0o600)
    else:
        _atomic_write_text(path, text, fsync_parent=True)
    return path.resolve()
