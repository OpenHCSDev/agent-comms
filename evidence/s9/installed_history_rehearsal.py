"""Read installed native evidence from existing saved sessions without replay/copy."""

import argparse
import json
from collections import Counter
from pathlib import Path

import agent_comms
from agent_comms.native_entries import NativeEntry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("owners", nargs="+")
    args = parser.parse_args()
    package = Path(agent_comms.__file__).resolve()
    assert package.is_relative_to(args.installed.resolve()), package
    rows = json.loads(args.registry.read_text())["threads"]
    receipts = []
    for owner in args.owners:
        session = Path(rows[owner]["session_file"])
        before = session.stat()
        kinds = Counter()
        count = largest = 0
        with session.open("rb") as stream:
            for count, raw in enumerate(stream, 1):
                assert raw.endswith(b"\n"), f"Incomplete record {count}"
                entry = NativeEntry.from_evidence(json.loads(raw))
                kinds[type(entry).__name__] += 1
                largest = max(largest, len(raw))
        after = session.stat()
        assert (before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_ino, after.st_size, after.st_mtime_ns
        ), "Saved source changed during read"
        receipts.append({
            "owner": owner, "source": str(session), "bytes": after.st_size,
            "records": count, "largest_record_bytes": largest, "kinds": dict(kinds),
            "read_only": True, "copied": False, "replayed": False,
        })
    print(json.dumps({"installed_package": str(package), "sessions": receipts}, indent=2))


if __name__ == "__main__":
    main()
