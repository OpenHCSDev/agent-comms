#!/usr/bin/env python3
"""Opt-in RED tests for missing pinned native session/owner commit CAS; no provider calls.

Run each case separately. An AssertionError is the expected observation against
Pi 0.85.1's pinned writer, not permission to patch production or activate PR48.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from agent_comms.declarations import Goal, Thread, ThreadRegistry  # noqa: E402

SCRIPT = Path(__file__).with_suffix(".mjs")
MANIFEST = Path(__file__).with_name("pi-native.sha256")


def saved_entries(session_file: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in Path(session_file).read_text().splitlines()]


def run_case(case: str, package: Path) -> None:
    subprocess.run(
        ["sha256sum", "--check", "--status", str(MANIFEST)],
        cwd=package,
        check=True,
        timeout=20,
    )
    env = {
        "HOME": str(Path.home()),
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "PI_NATIVE_PACKAGE_DIR": str(package),
    }
    with tempfile.TemporaryDirectory(prefix="pr48-native-writer-gap-") as directory:
        root = Path(directory)
        registry = ThreadRegistry(root / "registry.json")
        registry.register(
            Thread(
                name="owner",
                tags=frozenset(),
                worktree=str(root),
                pid=os.getpid(),
                goal=Goal("original task", "goal-old"),
            )
        )
        original_owner, original_epoch = registry.live_owner_with_epoch("owner")
        claimed, claimed_epoch = registry.claim_live_turn_with_epoch(
            original_owner, "turn-old", expected_epoch=original_epoch
        )
        assert claimed.active_turn is not None and claimed_epoch > original_epoch
        proc = subprocess.Popen(
            ["node", str(SCRIPT), "pending", str(root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            assert proc.stdout is not None and proc.stdin is not None
            captured_line = proc.stdout.readline()
            if not captured_line:
                raise RuntimeError(
                    f"Native capture exited: {proc.stderr.read() if proc.stderr else ''}"
                )
            captured = json.loads(captured_line)
            assert captured["phase"] == "captured"
            assert captured["capturedLeaf"]
            assert captured["branchLength"] >= 2

            invalidation: dict[str, object] = {}
            if case == "local-leaf":
                action = "append-local"
            elif case == "other-writer":
                written = subprocess.run(
                    ["node", str(SCRIPT), "external-append", captured["sessionFile"]],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=20,
                )
                appended = json.loads(written.stdout)
                assert appended["appendedId"] != captured["capturedLeaf"]
                saved = saved_entries(captured["sessionFile"])
                assert saved[-1]["id"] == appended["appendedId"]
                invalidation = {"externalWriterEntry": appended["appendedId"]}
                action = "continue"
            elif case == "owner-goal":
                # The registry, not JS labels, owns the goal and claimed turn.
                registry.register(
                    replace(registry.require("owner"), goal=Goal("new task", "goal-new"))
                )
                assert registry.require("owner").goal.id == "goal-new"
                current_epoch = registry.snapshot().owner_epochs["owner"]
                assert current_epoch != claimed_epoch
                invalidation = {
                    "goalBefore": claimed.goal.id if claimed.goal else None,
                    "goalAfter": registry.require("owner").goal.id,
                    "ownerEpochBefore": claimed_epoch,
                    "ownerEpochAfter": current_epoch,
                }
                action = "continue"
            else:
                raise ValueError("Unknown negative case")

            proc.stdin.write(action + "\n")
            proc.stdin.flush()
            stdout, stderr = proc.communicate(timeout=20)
            if proc.returncode != 0:
                raise RuntimeError(f"Native probe failed: {stderr}")
            result = json.loads(stdout.strip())
            assert result["phase"] == "committed"
            saved = saved_entries(result["sessionFile"])
            print(
                json.dumps(
                    {
                        "case": case,
                        "captured": captured,
                        "invalidation": invalidation,
                        "result": result,
                        "savedTail": [
                            {"id": row["id"], "parentId": row["parentId"], "type": row["type"]}
                            for row in saved[-2:]
                        ],
                        "stderr": stderr,
                    },
                    sort_keys=True,
                )
            )
            # The target safety invariant is deliberately RED on the pinned
            # native API: stale leaf, external writer, or owner/goal change
            # must cause refusal BEFORE mutation, but appendCompaction lacks CAS.
            assert (
                result["commitId"] is None
            ), f"UNSAFE {case}: native appendCompaction committed after preflight invalidation"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=("local-leaf", "other-writer", "owner-goal"))
    parser.add_argument(
        "--package", type=Path, required=True, help="verified read-only pinned Pi package directory"
    )
    args = parser.parse_args()
    run_case(args.case, args.package.resolve())


if __name__ == "__main__":
    main()
