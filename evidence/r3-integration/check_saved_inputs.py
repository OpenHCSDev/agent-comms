"""Compare old/new durable observations on owned copies, without replaying inputs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def snapshot(mode, base):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.input_disposition import AcpDeliveryCursors, InputDispositions

    result = {}
    for root in sorted(base.iterdir()):
        if not root.is_dir():
            continue
        try:
            if mode == "new":
                inputs = InputDispositions(root / InputDispositions.filename)
                cursors = AcpDeliveryCursors(root / AcpDeliveryCursors.filename)
                document = inputs.read()
                rows = FieldCodec.encode(document)["rows"]
                delivery = FieldCodec.encode(cursors.read())["rows"]
                # Exercise actual publication only in the disposable saved copy.
                inputs.replace(document)
                assert inputs.read() == document
            else:
                inputs = InputDispositions(root)
                rows = inputs._read()
                delivery = AcpDeliveryCursors(root)._read()
            # Optional notice/review defaults were historically omitted.
            for row in rows.values():
                row.setdefault("notice_dismissed", False)
                row.setdefault("goal_reviews", {})
            result[root.name] = {"inputs": rows, "cursors": delivery}
        except Exception as error:
            result[root.name] = {"error": repr(error)}
    print(json.dumps(result, sort_keys=True))


def compare(base, source):
    from agent_comms.store_files import _store_lock

    roots = (
        Path("/home/ts/.agent-comms"),
        Path("/var/tmp/agent-comms-live-20260927-wzjtqhza"),
        Path("/var/tmp/agent-comms-live-20260927-6_d_vdul"),
    )
    base.mkdir(parents=True, exist_ok=False)
    for index, root in enumerate(roots):
        target = base / str(index)
        target.mkdir()
        for name in ("input_dispositions.json", "acp_delivery_cursors.json"):
            path = root / name
            if path.exists():
                with _store_lock(path, shared=True):
                    shutil.copyfile(path, target / name)
    observations = {}
    for mode in ("old", "new"):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        if mode == "new":
            env["PYTHONPATH"] = str(source / "src")
        output = subprocess.check_output(
            [sys.executable, __file__, mode, str(base)], env=env, timeout=60,
        )
        observations[mode] = json.loads(output)
    result = {"equal": observations["old"] == observations["new"], "roots": []}
    for index, root in enumerate(roots):
        old, new = (observations[mode][str(index)] for mode in ("old", "new"))
        rows = old.get("inputs", {})
        item = {
            "source": str(root), "equal": old == new,
            "input_rows": len(rows), "delivery_cursors": len(old.get("cursors", {})),
            "unknown_rows": sum(row["status"] == "unknown" for row in rows.values()),
            "old_error": old.get("error"), "new_error": new.get("error"),
        }
        if old.get("error") or new.get("error"):
            result["equal"] = False
        result["roots"].append(item)
    result["live_input_sends"] = 0
    result["writes_only_to_owned_copies"] = True
    print(json.dumps(result, indent=2))
    if not result["equal"]:
        raise SystemExit(1)


if __name__ == "__main__":
    if sys.argv[1] in ("old", "new"):
        snapshot(sys.argv[1], Path(sys.argv[2]))
    else:
        compare(Path(sys.argv[1]), Path(sys.argv[2]))
