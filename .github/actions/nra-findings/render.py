"""The NRA gate's pull request comment, rendered from the verdict lines the gate prints on stderr.

The gate (nominal_refactor_advisor.audit.gate) owns every verdict line and which measures block;
this only selects those lines from whatever else reached stderr (installer timing, tool chatter)
and splits them. A warn-only measure announces itself with a ``(warns, never blocks)`` warning,
so its ``found`` lines are warn-only too; every other verdict line blocks.

    python -I render.py STDERR_FILE MARKER KIND SHA [--passed] > body.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ADVISORY = re.compile(r"^::warning(?: file=(?P<path>[^:]*))?::(?P<name>\w+) (?P<rest>\+\d+) \(warns, never blocks\)$")
BLOCKING = ("grew ", "introduced ", "missing introduced-site report for ", "done plan phase ")
FOUND = re.compile(r"^found (?P<name>\w+)")
# GitHub refuses a comment body over 65536 characters.
BUDGET = 60000


def fenced(lines: list[str], budget: int) -> str:
    kept, used = [], 0
    for line in lines:
        if used + len(line) + 1 > budget:
            kept.append(f"... {len(lines) - len(kept)} more lines in the job log")
            break
        kept.append(line)
        used += len(line) + 1
    return "```\n" + "\n".join(kept) + "\n```"


def main(stderr: Path, marker: str, kind: str, sha: str, passed: bool) -> str:
    lines = [line.rstrip() for line in stderr.read_text(errors="replace").splitlines()]
    advisory = {match["name"]: match for line in lines if (match := ADVISORY.match(line))}
    blocking, warns = [], []
    for line in lines:
        if match := ADVISORY.match(line):
            where = f"{match['path']}: " if match["path"] else ""
            warns.append(f"{where}{match['name']} {match['rest']}")
        elif found := FOUND.match(line):
            (warns if found["name"] in advisory else blocking).append(line)
        elif line.startswith(BLOCKING):
            blocking.append(line)

    head = f"{marker}\n**{kind}** at {sha}"
    if passed:
        body = [f"{head} passes."]
    elif blocking:
        body = [f"{head} blocks on {len(blocking)} lines:", fenced(blocking, BUDGET * 3 // 4)]
    else:
        tail = [line for line in lines if line][-40:]
        body = [f"{head} failed without a verdict line; the last lines of its stderr:",
                fenced(tail, BUDGET * 3 // 4)]
    if warns:
        body += [f"<details><summary>Warn-only, never blocks ({len(warns)})</summary>\n",
                 fenced(warns, BUDGET // 4 - 200), "</details>"]
    return "\n\n".join(body) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stderr", type=Path)
    parser.add_argument("marker")
    parser.add_argument("kind")
    parser.add_argument("sha")
    parser.add_argument("--passed", action="store_true", help="The gate step succeeded")
    args = parser.parse_args()
    sys.stdout.write(main(args.stderr, args.marker, args.kind, args.sha, args.passed))
