"""Real OS behavior for the shared child owner; no simulated subprocesses."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.child_process import (
    AttachedChild,
    BoundedRun,
    ChildOutcome,
    ExitedOutcome,
    IdentityMismatchError,
    ObservedProcess,
    ParentedProcess,
    Platform,
    ProcessIdentity,
    TimedOutOutcome,
)
from agent_comms.field_codec import FieldCodec

# The grandchild ignores TERM, so the owner must force retirement even after
# the group leader exits. Publishing both identities proves they really ran.
TREE = """import subprocess,sys,os,time,json
from pathlib import Path
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec
grandchild = ('import signal,time,sys; '
              'stop = signal.SIGBREAK if sys.platform=="win32" else signal.SIGTERM; '
              'signal.signal(stop, signal.SIG_IGN); print("ready",flush=True); time.sleep(60)')
child=subprocess.Popen([sys.executable,'-c',grandchild],stdout=subprocess.PIPE)
assert child.stdout.readline()==b'ready\\n'
identities=[FieldCodec.encode(ProcessIdentity.capture(p)) for p in (os.getpid(),child.pid)]
Path(sys.argv[1]).write_text(json.dumps(identities))
print('tree ready',flush=True)
time.sleep(60)
"""


def identities(path: Path) -> tuple[ProcessIdentity, ...]:
    return tuple(FieldCodec.decode(ProcessIdentity, row) for row in json.loads(path.read_text()))


async def ready(path: Path) -> None:
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_bounded_run_retires_real_child_and_defiant_grandchild(tmp_path: Path) -> None:
    receipt = tmp_path / "tree.json"
    result = await BoundedRun.run((sys.executable, "-c", TREE, str(receipt)), timeout=1)
    assert isinstance(result.outcome, TimedOutOutcome)
    assert result.stdout == b"tree ready\n"
    assert all(not member.alive() for member in identities(receipt))


@pytest.mark.asyncio
async def test_attached_stop_retires_real_child_and_defiant_grandchild(
    tmp_path: Path, monkeypatch
) -> None:
    import threading

    receipt = tmp_path / "tree.json"
    child = await AttachedChild.start((sys.executable, "-c", TREE, str(receipt)))
    original = child.platform.group_members
    scan_threads = []

    def observed_members(identity):
        scan_threads.append(threading.get_ident())
        return original(identity)

    monkeypatch.setattr(child.platform, "group_members", observed_members)
    try:
        await ready(receipt)
    finally:
        await child.stop()
    assert all(not member.alive() for member in identities(receipt))
    assert child.returncode is not None
    assert scan_threads and threading.get_ident() not in scan_threads


@pytest.mark.asyncio
async def test_detached_mismatch_refuses_signal_without_harming_real_child() -> None:
    child = ParentedProcess.launch((sys.executable, "-c", "import time; time.sleep(60)"))
    stale = ObservedProcess(replace(child.identity, start_time=child.identity.start_time + 1))
    try:
        assert not stale.alive()
        with pytest.raises(IdentityMismatchError):
            stale.force()
        with pytest.raises(IdentityMismatchError):
            await stale.stop()
        assert child.alive()
    finally:
        await child.stop()
    assert not child.alive()


@pytest.mark.asyncio
async def test_real_execution_and_external_returncode_family() -> None:
    for code in (0, 7):
        result = await BoundedRun.run(
            (sys.executable, "-c", f'import sys; print("ok"); sys.exit({code})'), timeout=5
        )
        assert result.outcome == ExitedOutcome(code)
        assert result.stdout == b"ok\n"
        assert result.outcome.successful == (code == 0)


@pytest.mark.asyncio
async def test_cancellation_retains_child_cleanup(tmp_path: Path) -> None:
    receipt = tmp_path / "tree.json"
    task = asyncio.create_task(
        BoundedRun.run((sys.executable, "-c", TREE, str(receipt)), timeout=10)
    )
    await ready(receipt)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(not member.alive() for member in identities(receipt))


def test_new_outcome_is_derived_and_field_encoded() -> None:
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class TestObservationOutcome(ChildOutcome):
        observation: str

        @property
        def successful(self) -> bool:
            return False

    try:
        value = TestObservationOutcome("observed")
        assert ChildOutcome.decode("test_observation") is TestObservationOutcome
        assert FieldCodec.decode(ChildOutcome, FieldCodec.encode(value)) == value
    finally:
        ChildOutcome.__registry__.pop("test_observation")


@pytest.mark.asyncio
async def test_namespace_deadline_kills_group_escaping_descendant(tmp_path: Path) -> None:
    from agent_comms.child_process import NamespaceContainment, NamespacedChild, SignaledOutcome

    if not isinstance(Platform.current(), NamespaceContainment):
        pytest.skip("This platform does not provide PID namespaces")
    receipt = tmp_path / "escaped"
    command = (
        sys.executable,
        "-c",
        """import subprocess,sys,time
code=('import os,sys,time; os.setsid(); '
      'open(sys.argv[1],"w").write("alive"); time.sleep(60)')
subprocess.Popen([sys.executable,'-c',code,sys.argv[1]])
time.sleep(60)
""",
        str(receipt),
    )
    child = await NamespacedChild.start(command, deadline=time.monotonic() + 2)
    try:
        await ready(receipt)
        assert NamespaceContainment.namespace_alive(child.namespace)
        async with asyncio.timeout(6):
            outcome = await child.wait()
            assert isinstance(outcome, SignaledOutcome)
    finally:
        await child.stop()
    assert not NamespaceContainment.namespace_alive(child.namespace)


@pytest.mark.asyncio
async def test_exec_failure_is_reported_by_real_exec_boundary(tmp_path: Path) -> None:
    from agent_comms.child_process import FailedToStartOutcome

    result = await BoundedRun.run((str(tmp_path / "missing-executable"),), timeout=2)
    assert isinstance(result.outcome, FailedToStartOutcome)


@pytest.mark.asyncio
async def test_namespace_watchdog_survives_launcher_death(tmp_path: Path) -> None:
    from agent_comms.child_process import NamespaceContainment

    if not isinstance(Platform.current(), NamespaceContainment):
        pytest.skip("This platform does not provide PID namespaces")
    receipt = tmp_path / "namespace.json"
    launcher = ParentedProcess.launch(
        (
            sys.executable,
            "-c",
            """import asyncio,json,sys,time
from pathlib import Path
from agent_comms.child_process import NamespacedChild
async def run():
    command=(sys.executable,'-c','import time; time.sleep(60)')
    child=await NamespacedChild.start(command,deadline=time.monotonic()+3)
    Path(sys.argv[1]).write_text(json.dumps(child.namespace))
    await asyncio.sleep(60)
asyncio.run(run())
""",
            str(receipt),
        )
    )
    try:
        await ready(receipt)
        namespace = json.loads(receipt.read_text())
        assert NamespaceContainment.namespace_alive(namespace)
        launcher.force()
        await launcher.wait()
        async with asyncio.timeout(6):
            while NamespaceContainment.namespace_alive(namespace):
                await asyncio.sleep(0.02)
    finally:
        if launcher.alive():
            await launcher.stop()


@pytest.mark.asyncio
async def test_parent_lifeline_loss_releases_real_sqlite_reader(tmp_path: Path) -> None:
    import sqlite3

    from agent_comms.child_process import ParentLifeline

    if sys.platform == "win32":
        pytest.skip("Recovery gateway uses POSIX peer sockets and inherited descriptors")
    database = tmp_path / "reader.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE state (value INTEGER)")
    receipt = tmp_path / "ready"
    with ParentLifeline() as life:
        child = await AttachedChild.start(
            (
                sys.executable,
                "-c",
                """
import sqlite3,sys,time
from pathlib import Path
from agent_comms.child_process import ParentLifeline
ParentLifeline.guard()
db=sqlite3.connect(sys.argv[1],isolation_level=None)
db.execute('BEGIN'); db.execute('SELECT * FROM state').fetchall()
Path(sys.argv[2]).write_text('ready')
time.sleep(60)
""",
                str(database),
                str(receipt),
            ),
            env=life.environment,
            pass_fds=(life.read_fd,),
        )
        await ready(receipt)
        with closing(sqlite3.connect(database, timeout=0.05, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO state VALUES (1)")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                db.execute("COMMIT")
            db.execute("ROLLBACK")
    try:
        async with asyncio.timeout(5):
            await child.wait()
        assert not child.alive()
        with sqlite3.connect(database, timeout=0.5) as db:
            db.execute("INSERT INTO state VALUES (2)")
            db.commit()
    finally:
        await child.stop()


def test_detached_reserves_identity_before_child_can_execute(tmp_path: Path) -> None:
    receipt = tmp_path / "started"
    recorded = []

    def reserve(identity: ProcessIdentity) -> None:
        assert not receipt.exists()
        assert identity.alive()
        recorded.append(identity)

    child = ParentedProcess.launch(
        (
            sys.executable,
            "-c",
            'import sys,time; open(sys.argv[1],"w").write("started"); time.sleep(60)',
            str(receipt),
        ),
        before_start=reserve,
    )
    try:
        assert recorded == [child.identity]
        stale = ObservedProcess(replace(child.identity, start_time=child.identity.start_time + 1))
        with pytest.raises(IdentityMismatchError):
            stale.stop_sync()
        assert child.alive()
    finally:
        child.stop_sync()
    assert not child.alive()


def test_reservation_failure_never_executes_and_reaps_child(tmp_path: Path) -> None:
    receipt = tmp_path / "should-not-exist"
    captured = []

    def reject(identity: ProcessIdentity) -> None:
        captured.append(identity)
        raise ValueError("reservation refused")

    with pytest.raises(ValueError, match="reservation refused"):
        ParentedProcess.launch(
            (
                sys.executable,
                "-c",
                "import sys; open(sys.argv[1], 'w').write('wrong')",
                str(receipt),
            ),
            before_start=reject,
        )
    assert len(captured) == 1
    assert not captured[0].alive()
    assert not receipt.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["run", "session"])
async def test_repeated_cancellation_joins_real_tree_before_return(
    tmp_path: Path, shape: str
) -> None:
    receipt = tmp_path / "tree.json"

    async def operation():
        command = (sys.executable, "-c", TREE, str(receipt))
        if shape == "run":
            await BoundedRun.run(command, timeout=20)
        else:
            async with BoundedRun.session(command, timeout=20):
                await asyncio.Event().wait()

    task = asyncio.create_task(operation())
    await ready(receipt)
    task.cancel()
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(not member.alive() for member in identities(receipt))


def test_inherited_deadline_preserves_real_parent_and_fd(tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("Inherited hard deadline requires Linux pidfds")
    destination = tmp_path / "inherited.txt"
    with destination.open("wb") as held:
        result = BoundedRun.run_inherited(
            (
                sys.executable,
                "-c",
                "import os,sys; assert os.getppid()==int(sys.argv[1]); "
                "os.write(int(sys.argv[2]),b'authority held'); print(sys.stdin.read(),end='')",
                str(os.getpid()),
                str(held.fileno()),
            ),
            deadline=time.monotonic() + 5,
            pass_fds=(held.fileno(),),
            input=b"native request",
        )
    assert result.outcome == ExitedOutcome(0)
    assert result.stdout == b"native request"
    assert destination.read_bytes() == b"authority held"


@pytest.mark.asyncio
async def test_inherited_deadline_survives_parent_death_and_releases_real_lock(
    tmp_path: Path,
) -> None:
    if sys.platform != "linux":
        pytest.skip("Inherited hard deadline requires Linux pidfds")
    import fcntl

    receipt = tmp_path / "child.json"
    lock = tmp_path / "authority.lock"
    launcher = ParentedProcess.launch(
        (
            sys.executable,
            "-c",
            r'''import json,os,sys,time,fcntl
from pathlib import Path
from agent_comms.child_process import BoundedRun
fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600)
fcntl.flock(fd,fcntl.LOCK_EX)
command=(sys.executable,'-c',"""import os,sys,time,json
from pathlib import Path
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec
assert os.getppid()==int(sys.argv[1])
os.fstat(int(sys.argv[2]))
Path(sys.argv[3]).write_text(json.dumps(FieldCodec.encode(ProcessIdentity.capture(os.getpid()))))
time.sleep(60)
""",str(os.getpid()),str(fd),sys.argv[2])
BoundedRun.run_inherited(command,deadline=time.monotonic()+4,pass_fds=(fd,))
''',
            str(lock),
            str(receipt),
        )
    )
    child = None
    try:
        await ready(receipt)
        child = FieldCodec.decode(ProcessIdentity, json.loads(receipt.read_text()))
        launcher.force()
        await launcher.wait()
        with lock.open("rb") as observer:
            with pytest.raises(BlockingIOError):
                fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            async with asyncio.timeout(7):
                while child.alive():
                    await asyncio.sleep(0.02)
            fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(observer, fcntl.LOCK_UN)
    finally:
        if launcher.alive():
            await launcher.stop()
        if child is not None and child.alive():
            await ObservedProcess(child).stop()


@pytest.mark.asyncio
async def test_discard_stderr_drains_past_transport_capacity_to_real_eof():
    command = (
        sys.executable,
        "-c",
        "import os; payload=b'x'*65536; "
        "[os.write(2,payload) for _ in range(128)]; print('drained',flush=True)",
    )
    async with BoundedRun.session(command, timeout=10) as child:
        drain = asyncio.create_task(child.discard_stderr())
        try:
            assert await child.stdout.readline() == b"drained\n"
            outcome = await child.wait()
            await drain
            assert outcome.successful
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
    assert not child.alive()


@pytest.mark.asyncio
async def test_parent_custody_reaps_but_observation_cannot_claim_exit_code():
    from agent_comms.child_process import DetachedExitOutcome

    child = ParentedProcess.launch((sys.executable, "-c", "import time; time.sleep(.1); exit(7)"))
    observed = ObservedProcess(child.identity)
    try:
        assert await child.wait() == ExitedOutcome(7)
        assert await observed.wait() == DetachedExitOutcome()
        with pytest.raises(IdentityMismatchError):
            observed.stop_sync()
    finally:
        child.close()


def test_parent_pipe_custody_closes_after_real_exception():
    with (
        pytest.raises(ValueError, match="caller failed"),
        Platform.current().launch(
            (sys.executable, "-c", "import time; time.sleep(60)"), ()
        ) as launch,
        launch.spawn(stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child,
    ):
        launch.release(child.identity)
        launch.verify()
        identity = child.identity
        stdout, stderr = child.process.stdout, child.process.stderr
        raise ValueError("caller failed")
    assert not identity.alive()
    assert stdout.closed and stderr.closed
