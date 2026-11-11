"""Qualify each changed production module as an independent cold import root."""

import argparse
import importlib
from pathlib import Path
import subprocess
import sys


def main() -> int:
    if len(sys.argv) == 2 and not sys.argv[1].startswith("--"):
        importlib.import_module(sys.argv[1])
        print(f"PASS {sys.argv[1]}", flush=True)
        return 0
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", type=Path, help="Retained complete source-path manifest")
    args = parser.parse_args()
    if args.paths is not None:
        names = args.paths.read_text().splitlines()
    else:
        changed = subprocess.run(
            ["git", "ls-files", "--modified", "--others", "--exclude-standard", "--", "src/agent_comms"],
            cwd=root, capture_output=True, text=True, check=True, timeout=15,
        )
        names = changed.stdout.splitlines()
    failures = 0
    count = 0
    for name in sorted(set(names)):
        path = Path(name)
        if path.suffix != ".py":
            continue
        module = ".".join(path.relative_to("src").with_suffix("").parts)
        count += 1
        try:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), module],
                cwd=root, capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            failures += 1
            print(f"TIMEOUT {module}", flush=True)
            continue
        print(result.stdout, end="", flush=True)
        if result.returncode:
            failures += 1
            print(f"FAIL {module}: exit {result.returncode}\n{result.stderr}", flush=True)
    print(f"Cold roots: {count - failures} passed / {failures} failed / {count} total", flush=True)
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
