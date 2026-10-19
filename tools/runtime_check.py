"""A built runtime's check of itself on a check root built from the rehearsal copy.

    agent-comms --root COPY check-root --threads T --into DIR -- \
        <runtime>/bin/python -I tools/runtime_check.py --root {root} --toad TOAD_TREE --output DIR

The installer runs this with the runtime under check, in the copy's home. It publishes
the check root as this home's route, makes one new thread with ``agent-comms new`` (new
threads get the one backend that serves them), and starts Toad's
``tools/acceptance.py`` on that thread. Once Toad shows the thread, it sends one DM as
the user and waits for the thread's answer on the bus; Toad then has to show that
answer. The result is ``OUTPUT/result.json``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from agent_comms.active_route import ActiveRoute, publish_active_route
from agent_comms.comms import wire

BIN = Path(sys.executable).parent


def cli(root: Path, *arguments: str | Path, cwd: Path) -> dict:
    completed = subprocess.run([BIN / "agent-comms", "--root", root, *map(str, arguments)],
                               cwd=cwd, capture_output=True, text=True, timeout=120)
    result = json.loads(completed.stdout or "{}")
    if completed.returncode or "error" in result:
        raise RuntimeError(f"agent-comms {arguments[0]}: {completed.stdout}{completed.stderr}")
    return result


def answer(root: Path, thread: str, user: str, token: str, project: Path, patience: int) -> dict:
    """The thread's DM to the user carrying ``token``."""
    waited = time.monotonic() + patience
    while time.monotonic() < waited:
        page = cli(root, "history", "--target", f"@{thread}", "--as", user, "--limit", "10", cwd=project)
        for message in page["dm"]:
            if message["from"] == thread and token in message["text"]:
                return message
        time.sleep(1)
    raise RuntimeError(f"{thread} did not answer within {patience}s")


class Toad:
    """Toad's acceptance on the check's thread, spoken to one line at a time."""

    def __init__(self, toad: Path, thread: str, output: Path):
        self.process = subprocess.Popen(
            [sys.executable, "-I", toad / "tools/acceptance.py", "--runtime", BIN,
             "--thread", thread, "--output", output],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def expect(self, word: str) -> None:
        line = self.process.stdout.readline().strip()
        if line != word:
            self.process.kill()
            raise RuntimeError(f"Toad's acceptance said {line!r}, not {word}; exit {self.process.wait()}")

    def say(self, line: str) -> None:
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def close(self) -> dict:
        self.process.stdin.close()
        result = json.loads(self.process.stdout.read().strip().splitlines()[-1])
        if self.process.wait():
            raise RuntimeError(f"Toad's acceptance failed: {result}")
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--toad", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--patience", type=int, default=300, help="Seconds the new thread has to answer")
    args = parser.parse_args()
    comms = wire(args.root)
    publish_active_route(ActiveRoute(comms.root, comms.bus.log.private_marker_unlocked().root_id))
    project = args.output / "project"
    project.mkdir(parents=True)
    thread = f"install-check-{time.time_ns()}"
    cli(args.root, "new", "--name", thread, "--project", project, cwd=project)
    try:
        toad = Toad(args.toad, thread, args.output / "toad")
        toad.expect("SHOWN")
        token = f"INSTALL_CHECK_{time.time_ns()}"
        started = time.monotonic()
        cli(args.root, "user-send", "--to", thread, "--body", f"Reply with {token}. Do not use tools.",
            "--worktree", project, cwd=project)
        user = comms.registry.user_thread().require(RuntimeError("user-send registered no user")).name
        reply = answer(args.root, thread, user, token, project, args.patience)
        answered = time.monotonic() - started
        toad.say(json.dumps(reply["text"]))
        toad.expect("SEEN")
        ui = toad.close()
    finally:
        cli(args.root, "stop", "--name", thread, cwd=project)
    result = json.dumps({"thread": thread, "model": cli(args.root, "thread", "--name", thread, cwd=project)["model"],
                         "reply": reply["text"], "reply_seconds": round(answered, 3), "toad": ui,
                         "evidence": str(args.output), "home": os.environ["HOME"]})
    # A check root's command shares its stdout with the CLI's own result.
    (args.output / "result.json").write_text(result)
    print(result)


if __name__ == "__main__":
    main()
