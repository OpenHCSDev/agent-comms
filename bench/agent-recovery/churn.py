"""Retrospective churn analysis: split a repository's history into agent arms
and compare lines touched against the net change those commits left behind.

An arm is identified by a commit-trailer substring (for example the
``Co-Authored-By: Claude`` trailer); every other commit in the window belongs
to the baseline arm.  Only commits before the first marked commit count for
the baseline, so the two arms never overlap in time.

Usage:
    python churn.py REPO [--since 2026-09-26] [--marker Claude] [--json]
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
from dataclasses import asdict, dataclass

SOURCE_ROOTS = ("src/", "tests/")
EVIDENCE_ROOTS = ("evidence/", ".artifacts/", "diagnostics/")


def bucket(path: str) -> str:
    if path.startswith(SOURCE_ROOTS):
        return "source"
    if path.startswith(EVIDENCE_ROOTS):
        return "evidence"
    if path.endswith(".md") or path.startswith("docs/"):
        return "docs"
    return "other"


@dataclass
class Arm:
    commits: int = 0
    first: int = 0
    last: int = 0
    source_added: int = 0
    source_deleted: int = 0
    evidence_added: int = 0
    evidence_deleted: int = 0
    docs_lines: int = 0
    files_edited_10_plus: int = 0

    @property
    def hours(self) -> float:
        return (self.last - self.first) / 3600

    @property
    def net(self) -> int:
        return self.source_added - self.source_deleted

    @property
    def rewrite_ratio(self) -> float:
        return (self.source_added + self.source_deleted) / max(1, abs(self.net))


def analyse(repo: str, since: str, marker: str) -> dict[str, Arm]:
    log = subprocess.run(
        [
            "git", "-C", repo, "log", "--all", "--no-merges", f"--since={since}",
            "--format=%x01%H|%ct|%(trailers:key=Co-Authored-By,valueonly,separator=;)",
            "--numstat",
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    commits = []
    for block in log.split("\x01")[1:]:
        lines = block.strip("\n").split("\n")
        _, timestamp, trailers = lines[0].split("|", 2)
        commits.append((int(timestamp), marker in trailers, lines[1:]))
    marked = [t for t, is_marked, _ in commits if is_marked]
    cutoff = min(marked) if marked else float("inf")
    arms = {"baseline": Arm(), "marked": Arm()}
    edits = {name: collections.Counter() for name in arms}
    for timestamp, is_marked, numstat in commits:
        if not is_marked and timestamp >= cutoff:
            continue
        name = "marked" if is_marked else "baseline"
        arm = arms[name]
        arm.commits += 1
        arm.first = min(arm.first or timestamp, timestamp)
        arm.last = max(arm.last, timestamp)
        for row in numstat:
            parts = row.split("\t")
            if len(parts) != 3 or parts[0] == "-":
                continue
            added, deleted, path = int(parts[0]), int(parts[1]), parts[2]
            kind = bucket(path)
            if kind == "source":
                arm.source_added += added
                arm.source_deleted += deleted
                edits[name][path] += 1
            elif kind == "evidence":
                arm.evidence_added += added
                arm.evidence_deleted += deleted
            elif kind == "docs":
                arm.docs_lines += added + deleted
    for name, arm in arms.items():
        arm.files_edited_10_plus = sum(1 for n in edits[name].values() if n >= 10)
    return arms


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo")
    parser.add_argument("--since", default="2026-09-26")
    parser.add_argument("--marker", default="Claude")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    arms = analyse(args.repo, args.since, args.marker)
    if args.json:
        print(json.dumps({k: asdict(v) | {"net": v.net, "rewrite_ratio": v.rewrite_ratio, "hours": v.hours} for k, v in arms.items()}, indent=1))
        return
    print(f"{'arm':9} {'commits':>7} {'hours':>6} {'src +':>8} {'src -':>8} {'net':>8} {'rewrite':>7} {'10+ edits':>9} {'evid +':>9} {'evid -':>9} {'docs':>7}")
    for name, arm in arms.items():
        print(
            f"{name:9} {arm.commits:7} {arm.hours:6.0f} {arm.source_added:8} {arm.source_deleted:8} "
            f"{arm.net:+8} {arm.rewrite_ratio:7.1f} {arm.files_edited_10_plus:9} {arm.evidence_added:9} {arm.evidence_deleted:9} {arm.docs_lines:7}"
        )


if __name__ == "__main__":
    main()
