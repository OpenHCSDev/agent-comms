"""One-shot quiet-cutover carrier. Run only on explicit copied/offline JSON files.

No production reader accepts the retired thread/cursor shape. Keep this tool
until installation applies the reviewed carrier, then delete it before merge.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.native_pi import NativeContextRecord


def carry(value):
    if isinstance(value, list):
        return [carry(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: carry(item) for key, item in value.items()}
    thread = {"name", "tags", "worktree", "created_at"} <= result.keys()
    cursor = {"kind", "session_file", "offset"} <= result.keys() and result["kind"] in ("native", "wire")
    read = {"root", "requested_name", "records_cut", "thread", "session_file"} <= result.keys()
    if (thread or cursor or read) and "runtime" not in result:
        path = result.pop("session_file", None)
        result["runtime"] = {
            "backend": result.pop("backend", "pi_native"),
            "location": {"path": path} if path else None,
        }
    proof = {key for _, key in FieldCodec.record_fields(NativeContextRecord)} | {"session_file"}
    if proof <= result.keys():
        result["source_path"] = result.pop("session_file")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", type=Path, nargs="+")
    args = parser.parse_args()
    for path in args.files:
        original = json.loads(path.read_text())
        converted = carry(original)
        if converted != original:
            temporary = path.with_name(path.name + ".runtime-cutover")
            with temporary.open("x") as stream:
                json.dump(converted, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
            temporary.chmod(path.stat().st_mode)
            temporary.replace(path)


if __name__ == "__main__":
    main()
