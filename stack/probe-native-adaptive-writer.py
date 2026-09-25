#!/usr/bin/env python3
"""Opt-in RED tests for missing pinned native session/owner commit CAS; no provider calls.

Run each case separately. An AssertionError is the expected observation against
Pi 0.85.1's pinned writer, not permission to patch production or activate PR48.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from agent_comms.declarations import (  # noqa: E402
    Goal,
    RelationViolationError,
    Thread,
    ThreadRegistry,
)

SCRIPT = Path(__file__).with_suffix(".mjs")
MANIFEST = Path(__file__).with_name("pi-native.sha256")
PROTOTYPE_SHA = "d9f0e3b3e6ff8975a13d6a5f07cb88b13e399003d8725c409829edd4425afbe6"


def saved_entries(session_file: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in Path(session_file).read_text().splitlines()]


def read_capture_line(proc: subprocess.Popen[str], *, timeout: float = 10.0) -> str:
    """Bound BOTH time and bytes before trusting a child startup receipt."""
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    captured = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        while len(captured) < 4096:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError("Native capture produced no complete line before deadline")
            chunk = os.read(proc.stdout.fileno(), 4096 - len(captured))
            if not chunk:
                raise EOFError("Native capture closed stdout before a complete line")
            captured.extend(chunk)
            if b"\n" in captured:
                line, trailing = bytes(captured).split(b"\n", 1)
                if trailing:
                    raise ValueError("Native capture emitted unexpected extra output")
                return line.decode("utf-8")
    raise ValueError("Native capture record exceeds 4096 bytes")


def self_test_no_line() -> None:
    """A child that writes only a partial line cannot strand this probe."""
    script = "import sys,time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(30)"
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    try:
        try:
            read_capture_line(child, timeout=0.3)
        except TimeoutError:
            pass
        else:
            raise AssertionError("A no-line child cannot produce a capture receipt")
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=3)
    elapsed = time.monotonic() - started
    assert child.returncode is not None and elapsed < 3, "No-line child did not settle promptly"
    print(f"no-line deadline PASS: child reaped in {elapsed:.2f}s")


def run_case(case: str, package: Path, *, prototype: bool) -> None:
    if prototype:
        for row in MANIFEST.read_text().splitlines():
            expected, relative = row.split("  ", 1)
            if relative == "dist/core/session-manager.js":
                expected = PROTOTYPE_SHA
            actual = hashlib.sha256((package / relative).read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f"Disposable native package mismatch: {relative}")
    else:
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
    if case == "unknown-write":
        env["PR48_PROBE_FAIL_AFTER_WRITE"] = "1"  # Only the disposable patch reads this.
    with tempfile.TemporaryDirectory(prefix="pr48-native-writer-gap-") as directory:
        root = Path(directory)
        registry = None
        claimed = None
        claimed_epoch = None
        if case in {"owner-goal", "owner-stop", "positive"}:
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
            try:
                captured_line = read_capture_line(proc)
            except (TimeoutError, EOFError, ValueError) as error:
                if proc.poll() is None:
                    proc.kill()
                _, stderr = proc.communicate(timeout=3)
                raise RuntimeError(
                    f"Native capture failed: {error}; stderr={stderr[:2048]}"
                ) from error
            captured = json.loads(captured_line)
            assert captured["phase"] == "captured"
            assert captured["capturedLeaf"]
            assert captured["branchLength"] >= 2

            invalidation: dict[str, object] = {}
            attestation: dict[str, object] | None = None
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
            elif case in {"owner-goal", "owner-stop", "positive"}:
                if case == "owner-goal":
                    # The registry, not JS labels, owns the goal and claimed turn.
                    assert (
                        registry is not None and claimed is not None and claimed_epoch is not None
                    )
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
                elif case == "owner-stop":
                    assert registry is not None and claimed_epoch is not None
                    registry.unregister("owner")
                    assert not registry.status("owner").active
                    invalidation = {"ownerEpochBefore": claimed_epoch, "statusAfter": "stopped"}
                else:
                    assert (
                        registry is not None and claimed is not None and claimed_epoch is not None
                    )
                action = "continue-owner-goal"
                # Commit-time Python re-attestation (blocker-1 design): the
                # recheck fails closed for owner-goal/owner-stop, so no valid
                # attestation can reach the native bridge for these cases.
                try:
                    receipt = registry.attest_owner_compaction(
                        claimed,
                        claimed_epoch,
                        claimed.active_turn.id if claimed.active_turn else "",
                        expected_goal_id=claimed.goal.id if claimed.goal else "",
                        expected_goal_revision=claimed.goal.revision if claimed.goal else -1,
                        correction_revision=7,
                        session_file=str(captured["sessionFile"]),
                        session_leaf=str(captured["capturedLeaf"]),
                        session_revision=str(captured["sessionRevision"]),
                    )
                    attestation = asdict(receipt)
                except RelationViolationError:
                    attestation = None
                if case == "owner-goal":
                    assert attestation is None, "Mutated goal must not re-attest"
                if case == "owner-stop":
                    assert attestation is None, "Stopped owner must not re-attest"
                if case == "positive":
                    assert attestation is not None, "Live owner must re-attest"
            elif case == "crash-lock":
                holder = subprocess.Popen(
                    ["node", str(SCRIPT), "hold-lock", captured["sessionFile"]],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                try:
                    assert json.loads(read_capture_line(holder, timeout=5))["phase"] == "lock-held"
                finally:
                    if holder.poll() is None:
                        holder.kill()
                    holder.communicate(timeout=3)
                assert Path(captured["sessionFile"] + ".pr48-writer.lock").exists()
                invalidation = {"holderTerminated": holder.returncode is not None}
                action = "continue"
            elif case == "positive":
                action = "continue"
            elif case == "unknown-write":
                action = "probe-unknown-write"
            else:
                raise ValueError("Unknown negative case")

            proc.stdin.write(action + "\n")
            if attestation is not None:
                proc.stdin.write(json.dumps(attestation) + "\n")
            proc.stdin.flush()
            stdout, stderr = proc.communicate(timeout=20)
            if proc.returncode != 0:
                raise RuntimeError(f"Native probe failed: {stderr}")
            result = json.loads(stdout.strip())
            assert result["phase"] == "committed"
            saved = saved_entries(result["sessionFile"])
            if case == "crash-lock":
                # Blocker-4 policy: the stale lock is permanent for writers;
                # only an explicit operator action on the lock file restores
                # service, and the recovered append must succeed and commit.
                recovered = subprocess.run(
                    ["node", str(SCRIPT), "operator-recover", captured["sessionFile"]],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=20,
                )
                outcome = json.loads(recovered.stdout)
                assert outcome["phase"] == "recovered" and outcome["appendedId"]
                recovered_entries = saved_entries(captured["sessionFile"])
                assert recovered_entries[-1]["id"] == outcome["appendedId"]
                assert not Path(captured["sessionFile"] + ".pr48-writer.lock").exists()
                invalidation = {
                    **invalidation,
                    "operatorRecovery": outcome["appendedId"],
                }
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
            if case == "positive":
                assert prototype, "Positive guarded append requires the disposable prototype"
                assert result["commitId"] is not None and result["commitError"] is None
                assert saved[-1]["id"] == result["commitId"]
            else:
                # These remain RED on pinned stock Pi. The disposable writer
                # refuses before mutation except the injected post-write case:
                # bytes may be visible but the outcome is unknown, never retried.
                # Owner-scoped calls remain denied until a canonical bridge exists.
                assert (
                    result["commitId"] is None
                ), f"UNSAFE {case}: native appendCompaction committed after preflight invalidation"
                assert result["commitError"] is not None
                if prototype:
                    expected = {
                        "local-leaf": "Invalid native compaction witness",
                        "other-writer": "Native compaction source changed",
                        "owner-goal": "Canonical Python owner commit attestation unavailable",
                        "owner-stop": "Canonical Python owner commit attestation unavailable",
                        "crash-lock": "Native session writer lock unavailable",
                        "unknown-write": "Native compaction commit outcome unknown",
                    }[case]
                    assert expected in result["commitError"], result["commitError"]
                    if case == "unknown-write":
                        assert result["leafAfterCommit"] == captured["capturedLeaf"]
                        assert saved[-1]["type"] == "compaction", "Post-write bytes may be visible"
                        assert "Native session writer changed" in result["subsequentError"]
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "case",
        choices=(
            "local-leaf",
            "other-writer",
            "owner-goal",
            "owner-stop",
            "crash-lock",
            "unknown-write",
            "positive",
            "self-test-no-line",
        ),
    )
    parser.add_argument(
        "--package", type=Path, help="verified read-only pinned Pi package directory"
    )
    parser.add_argument(
        "--prototype", action="store_true", help="expect isolated patched native copy"
    )
    args = parser.parse_args()
    if args.case == "self-test-no-line":
        self_test_no_line()
        return
    if args.package is None:
        parser.error("--package is required for native cases")
    run_case(args.case, args.package.resolve(), prototype=args.prototype)


if __name__ == "__main__":
    main()
