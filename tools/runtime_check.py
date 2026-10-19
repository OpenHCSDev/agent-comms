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
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import ClassVar, get_args, get_origin, get_type_hints

from agent_comms.active_route import ActiveRoute, publish_active_route
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.history_result import DmHistoryPageResult
from agent_comms.messages import Message

BIN = Path(sys.executable).parent


def declared(kind: object, data: object) -> object:
    """``data`` cut to the fields ``kind`` declares, through nested records and tuples of them."""
    match data:
        case dict() if is_dataclass(kind):
            hints = get_type_hints(kind)
            return {
                key: declared(hints[entry.name], data[key])
                for entry in fields(kind)
                if (key := FieldCodec.wire_key(entry)) in data
            }
        case list() if get_origin(kind) is tuple:
            return [declared(get_args(kind)[0], item) for item in data]
    return data


def known[R](record: type[R], data: object) -> R:
    """``record`` decoded once from a CLI result that carries fields it does not read."""
    return FieldCodec.decode(record, declared(record, data))


@dataclass(frozen=True)
class Named:
    name: str


@dataclass(frozen=True)
class DmPage:
    """One page of a direct conversation, newest first."""

    messages: tuple[Message, ...] = field(metadata={"wire_name": DmHistoryPageResult.messages_key})

    def carrying(self, sender: str, token: str) -> tuple[Message, ...]:
        """``sender``'s messages carrying ``token``; the request that asked for it is the user's."""
        return tuple(
            message
            for message in self.messages
            if message.sender == sender and token in message.body
        )


@dataclass(frozen=True)
class ThreadModel:
    model: str


@dataclass(frozen=True)
class ToadAcceptance:
    """What Toad's acceptance passed, and where it kept its evidence."""

    plain: str
    shown: str
    answer: str
    channels: str
    filtered_return: str
    evidence: str


@dataclass(frozen=True)
class RuntimeChecked:
    thread: str
    model: str
    reply: str
    reply_seconds: float
    toad: ToadAcceptance
    evidence: str
    home: str

    name: ClassVar[str] = "result.json"

    @classmethod
    def read(cls, output: Path) -> RuntimeChecked:
        # A check root's command shares its stdout with the CLI's own result.
        return FieldCodec.decode(cls, FieldCodec.parse((output / cls.name).read_text()))

    def write(self, output: Path) -> None:
        (output / self.name).write_text(json.dumps(FieldCodec.encode(self)))


def command(root: Path, *arguments: str | Path, cwd: Path) -> str:
    completed = subprocess.run(
        [BIN / "agent-comms", "--root", root, *map(str, arguments)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode:
        raise RuntimeError(f"agent-comms {arguments[0]}: {completed.stdout}{completed.stderr}")
    return completed.stdout


def read[R](root: Path, record: type[R], *arguments: str | Path, cwd: Path) -> R:
    return known(record, FieldCodec.parse(command(root, *arguments, cwd=cwd)))


def answer(root: Path, thread: str, user: str, token: str, project: Path, patience: int) -> Message:
    """The thread's DM to the user carrying ``token``."""
    waited = time.monotonic() + patience
    while time.monotonic() < waited:
        page = read(
            root,
            DmPage,
            "history",
            "--target",
            f"@{thread}",
            "--as",
            user,
            "--limit",
            "10",
            cwd=project,
        )
        if carrying := page.carrying(thread, token):
            return carrying[0]
        time.sleep(1)
    raise RuntimeError(f"{thread} did not answer within {patience}s")


class Toad:
    """Toad's acceptance on the check's thread, spoken to one line at a time."""

    def __init__(self, toad: Path, thread: str, output: Path):
        requests, self.requests_end = os.pipe()
        self.answers_end, answers = os.pipe()
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                toad / "tools/acceptance.py",
                "--runtime",
                BIN,
                "--thread",
                thread,
                "--output",
                output,
            ],
            stdin=requests,
            stdout=answers,
        )
        os.close(requests)
        os.close(answers)
        self.requests = os.fdopen(self.requests_end, "w")
        self.answers = os.fdopen(self.answers_end)

    def expect(self, word: str) -> None:
        line = self.answers.readline().strip()
        if line != word:
            self.process.kill()
            raise RuntimeError(
                f"Toad's acceptance said {line!r}, not {word}; exit {self.process.wait()}"
            )

    def say(self, line: str) -> None:
        self.requests.write(line + "\n")
        self.requests.flush()

    def close(self) -> ToadAcceptance:
        self.requests.close()
        result = FieldCodec.decode(
            ToadAcceptance, FieldCodec.parse(self.answers.read().strip().splitlines()[-1])
        )
        if self.process.wait():
            raise RuntimeError(f"Toad's acceptance failed: {result}")
        return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--toad", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--patience", type=int, default=300, help="Seconds the new thread has to answer"
    )
    args = parser.parse_args()
    comms = wire(args.root)
    publish_active_route(ActiveRoute(comms.root, comms.bus.log.private_marker_unlocked().root_id))
    project = args.output / "project"
    project.mkdir(parents=True)
    thread = f"install-check-{time.time_ns()}"
    command(args.root, "new", "--name", thread, "--project", project, cwd=project)
    try:
        toad = Toad(args.toad, thread, args.output / "toad")
        toad.expect("SHOWN")
        token = f"INSTALL_CHECK_{time.time_ns()}"
        started = time.monotonic()
        command(
            args.root,
            "user-send",
            "--to",
            thread,
            "--body",
            f"Reply with {token}. Do not use tools.",
            "--worktree",
            project,
            cwd=project,
        )
        user = (
            comms.registry.user_thread().require(RuntimeError("user-send registered no user")).name
        )
        reply = answer(args.root, thread, user, token, project, args.patience)
        answered = time.monotonic() - started
        toad.say(json.dumps(reply.body))
        toad.expect("SEEN")
        ui = toad.close()
    finally:
        command(args.root, "stop", "--name", thread, cwd=project)
    result = RuntimeChecked(
        thread,
        read(args.root, ThreadModel, "thread", "--name", thread, cwd=project).model,
        reply.body,
        round(answered, 3),
        ui,
        str(args.output),
        str(Path.home()),
    )
    result.write(args.output)
    print(json.dumps(FieldCodec.encode(result)))


if __name__ == "__main__":
    main()
