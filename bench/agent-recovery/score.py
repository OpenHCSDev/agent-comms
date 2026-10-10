"""Score one run branch against its task snapshot.

Scores, in priority order:
  1. goal metric (task's metric command, run at snapshot and at the run tip);
  2. behaviour preserved: tests passing at the snapshot still pass at the tip;
  3. structure: net source lines, rewrite ratio, files edited 10+ times;
  4. bloat: evidence/artifact and docs lines added;
  5. claims: numbers stated in commit messages, listed for audit against (1).

Usage:
    python score.py TASK_ID CHECKOUT RUN_BRANCH [--no-metric] [--no-tests]
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path

from churn import bucket


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def at(repo: Path, rev: str):
    path = Path(tempfile.mkdtemp(prefix="score-"))
    git(repo, "worktree", "add", "--detach", str(path), rev)
    return path


def metric(command: str, tree: Path, harness: Path) -> float | None:
    # The harness comes from the bench checkout, never from the agent's tree.
    subprocess.run(["rm", "-rf", tree / "bench"], check=True)
    subprocess.run(["cp", "-r", harness, tree / "bench"], check=True)
    result = subprocess.run(command, shell=True, cwd=tree, capture_output=True, text=True)
    try:
        return float(json.loads(result.stdout.strip().splitlines()[-1])["value"])
    except (ValueError, IndexError, KeyError):
        return None


def passing(command: str, tree: Path) -> set[str]:
    result = subprocess.run(f"{command} -rA -p no:cacheprovider", shell=True, cwd=tree, capture_output=True, text=True)
    return {line.split()[1] for line in result.stdout.splitlines() if line.startswith("PASSED ")}


def structure(repo: Path, base: str, tip: str) -> dict:
    added = deleted = evidence = docs = 0
    edits: collections.Counter[str] = collections.Counter()
    for block in git(repo, "log", "--no-merges", "--format=%x01", "--numstat", f"{base}..{tip}").split("\x01")[1:]:
        for row in block.strip().splitlines():
            parts = row.split("\t")
            if len(parts) != 3 or parts[0] == "-":
                continue
            a, d, path = int(parts[0]), int(parts[1]), parts[2]
            kind = bucket(path)
            if kind == "source":
                added += a
                deleted += d
                edits[path] += 1
            elif kind == "evidence":
                evidence += a
            elif kind == "docs":
                docs += a
    net = added - deleted
    return {
        "commits": int(git(repo, "rev-list", "--count", "--no-merges", f"{base}..{tip}")),
        "source_added": added,
        "source_deleted": deleted,
        "source_net": net,
        "rewrite_ratio": round((added + deleted) / max(1, abs(net)), 2),
        "files_edited_10_plus": sum(1 for n in edits.values() if n >= 10),
        "evidence_lines_added": evidence,
        "docs_lines_added": docs,
    }


NUMBER = re.compile(r"\d+(?:\.\d+)?\s?(?:ms|s|%|x|MB|GB|lines)\b")


def claims(repo: Path, base: str, tip: str) -> list[str]:
    rows = []
    for message in git(repo, "log", "--format=%h %B%x00", f"{base}..{tip}").split("\x00"):
        found = NUMBER.findall(message)
        if found:
            rows.append(f"{message.split()[0]}: {', '.join(found)}")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("checkout", type=Path)
    parser.add_argument("branch")
    parser.add_argument("--no-metric", action="store_true")
    parser.add_argument("--no-tests", action="store_true")
    args = parser.parse_args()
    here = Path(__file__).parent
    task = next(t for t in tomllib.loads((here / "tasks.toml").read_text())["task"] if t["id"] == args.task)
    base, tip = task["snapshot"], args.branch
    report: dict = {"task": task["id"], "branch": tip, "structure": structure(args.checkout, base, tip)}
    if not (args.no_metric and args.no_tests):
        before, after = at(args.checkout, base), at(args.checkout, tip)
        if not args.no_metric and task["metric"]:
            report["metric"] = {
                "before": metric(task["metric"], before, here.parent),
                "after": metric(task["metric"], after, here.parent),
                "better": task["metric_better"],
            }
        if not args.no_tests:
            kept, now = passing(task["tests"], before), passing(task["tests"], after)
            report["tests"] = {"passing_before": len(kept), "passing_after": len(now), "regressed": sorted(kept - now)}
    report["claims_to_audit"] = claims(args.checkout, base, tip)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
