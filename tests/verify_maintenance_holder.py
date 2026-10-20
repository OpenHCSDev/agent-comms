"""Real detached live-phase/SIGKILL proof; reuse installed dependencies, never stage/build.

This exercises upgrade_bus.live, its detached launcher and the real Core CLI on
an isolated copied bus. Full upgrade/rehearsal preparation belongs to the merger.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tools")]

from agent_comms.child_process import Platform, ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.maintenance_barrier import DeadHolderReleasedPhase, MaintenanceState
from upgrade_bus import Revisions, Stack, copy_bus, detached_live, live

SCRATCH = Path("/home/ts/.cache/agent-scratch/pi-worker-cpu/p281/holder-death")
BUS = SCRATCH / "copy"


@dataclass(frozen=True)
class ExistingStack(Stack):
    @property
    def python(self) -> Path:
        return Path(sys.executable)


STACK = ExistingStack(ROOT, SCRATCH / "home")


def command(operation: Callable[[], None]) -> tuple[str, ...]:
    return (
        sys.executable,
        "-c",
        f"from verify_maintenance_holder import {operation.__name__}; {operation.__name__}()",
    )


def environment() -> dict[str, str]:
    return STACK.environment(ROOT / "tests", ROOT / "tools")


def busy_owner() -> None:
    comms = Comms(BUS)
    owner = comms.registry.new_thread(
        "busy", frozenset(), str(ROOT), process_identity=ProcessIdentity.capture(os.getpid())
    )
    comms.registry.declare(owner)
    assert comms.agents.begin_turn("busy", "p281-real-busy-turn")
    signal.signal(signal.SIGTERM, lambda *_: None)
    print("busy", flush=True)
    sys.stdin.read()


def live_worker() -> None:
    # Exactly the live installation phase, not a rehearse-only run. Empty pending
    # steps need no new revision worktree. The real busy owner holds its drain.
    head = subprocess.check_output(('git', 'rev-parse', 'HEAD'), cwd=ROOT, text=True).strip()
    live(ROOT, BUS, head, head, (), STACK, STACK,
         Revisions(ROOT, SCRATCH / 'unused-revisions'), 'pi-worker-cpu')


def caller() -> None:
    raise SystemExit(detached_live(command(live_worker), BUS))


def cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            "-c",
            "from agent_comms.cli import main; raise SystemExit(main())",
            "--root",
            str(BUS),
            *arguments,
        ),
        cwd=ROOT,
        env=environment(),
        capture_output=True,
        text=True,
        timeout=30,
    )


def digest(root: Path) -> str:
    value = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            value.update(str(path.relative_to(root)).encode())
            value.update(path.read_bytes())
    return value.hexdigest()


def probe() -> None:
    SCRATCH.mkdir(mode=0o700)
    STACK.home.mkdir(mode=0o700)
    original = SCRATCH / "original"
    source = Comms(original)
    for name in ("sender", "receiver"):
        source.registry.declare(source.registry.new_thread(name, frozenset(), str(ROOT)))
    before = digest(original)
    copy_bus(original, BUS)
    busy = subprocess.Popen(
        command(busy_owner),
        env=environment(),
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    observer = None
    members: tuple[ProcessIdentity, ...] = ()
    try:
        assert busy.stdout is not None and busy.stdout.readline().strip() == "busy"
        observer = subprocess.Popen(
            command(caller),
            env=environment(),
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Use the barrier's declared path, not a second hold file.
        from agent_comms.maintenance_barrier import MaintenanceBarrier

        state_path = MaintenanceBarrier(BUS / "registry.json").state_path
        deadline = time.monotonic() + 30
        while not state_path.exists():
            assert time.monotonic() < deadline, "Live installer did not enter drain"
            assert observer.poll() is None, "Caller exited before entering drain"
            time.sleep(0.01)
        held = FieldCodec.decode(MaintenanceState, FieldCodec.parse(state_path.read_text()))
        assert held.phase.declared_name == "draining"
        worker = ProcessIdentity.capture(held.operator.pid)
        assert worker == held.operator and worker.pid != observer.pid
        assert os.getsid(worker.pid) == worker.pid != os.getsid(observer.pid)
        # Kill the calling session first: its separately recorded worker survives.
        os.killpg(observer.pid, signal.SIGKILL)
        observer.wait(timeout=5)
        assert worker.alive(), "Calling-session death killed the detached installer"
        print(
            f"caller killed; detached holder alive pid={worker.pid} start_time={worker.start_time}"
        )
        # Freeze the actual installer job at its recorded drain, then SIGKILL the
        # job. No stopped CLI descendant can retain a lock or consume recovery.
        os.killpg(worker.pid, signal.SIGSTOP)
        members = Platform.current().group_members(worker)
        stopped = FieldCodec.decode(MaintenanceState, FieldCodec.parse(state_path.read_text()))
        assert stopped.phase.declared_name == "draining"
        os.killpg(worker.pid, signal.SIGKILL)
        while worker.alive():
            assert time.monotonic() < deadline, "SIGKILL did not retire holder"
            time.sleep(0.01)
        first = cli("send", "--from", "sender", "--to", "receiver", "--body", "p281-refused-input")
        print(f"first send rc={first.returncode}: {first.stdout.strip()}")
        assert first.returncode != 0 and "released dead holder" in first.stdout
        release_bytes = state_path.read_bytes()
        released = FieldCodec.decode(MaintenanceState, FieldCodec.parse(release_bytes.decode()))
        assert released.phase is DeadHolderReleasedPhase and released.operator == held.operator
        second = cli(
            "send", "--from", "sender", "--to", "receiver", "--body", "p281-accepted-input"
        )
        print(f"second send rc={second.returncode}: {second.stdout.strip()}")
        assert second.returncode == 0, second.stderr
        history = cli("history", "--limit", "10")
        assert history.returncode == 0 and "p281-accepted-input" in history.stdout
        assert "p281-refused-input" not in history.stdout
        assert state_path.read_bytes() == release_bytes
        assert digest(original) == before
        print("release record: " + release_bytes.decode().strip())
        print(
            "original unchanged; accepted message retained; rehearsal/environment preparation not run"
        )
    finally:
        for identity in members:
            if identity.alive():
                Platform.current().send(identity, signal.SIGKILL)
        if observer is not None and observer.poll() is None:
            observer.kill()
            observer.wait(timeout=5)
        if busy.poll() is None:
            busy.kill()
        busy.wait(timeout=5)
        if busy.stdin is not None:
            busy.stdin.close()
        if busy.stdout is not None:
            busy.stdout.close()
        if busy.stderr is not None:
            busy.stderr.close()


if __name__ == "__main__":
    probe()
