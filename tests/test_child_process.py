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
    ChildRetirementCancelled,
    ChildRetirementError,
    ChildRetirementFailed,
    ExitedOutcome,
    GroupMember,
    IdentityMismatchError,
    ObservedProcess,
    ParentedProcess,
    Platform,
    ProcessIdentity,
    TimedOutOutcome,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.launch_runtime import LaunchRuntime

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
    acknowledgement = tmp_path / "tree.json"
    async with BoundedRun.session((sys.executable, "-c", TREE, str(acknowledgement)), timeout=30) as child:
        # The child announces both live identities; machine load is not readiness.
        assert await child.stdout.readline() == b"tree ready\n"
        outcome = await child.finish()
        assert isinstance(outcome, TimedOutOutcome)
        assert all(not member.alive() for member in identities(acknowledgement))


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


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
@pytest.mark.parametrize("owner", (
    pytest.param(lambda process, identity: ObservedProcess(identity), id="stored-observed"),
    pytest.param(ParentedProcess, id="parented"),
))
@pytest.mark.parametrize("retire", (
    pytest.param(lambda child: asyncio.to_thread(child.stop_sync), id="sync"),
    pytest.param(lambda child: child.stop(), id="async"),
))
@pytest.mark.asyncio
async def test_stop_shared_process_group_member_without_signalling_neighbors(owner, retire):
    command = (sys.executable, "-c", "import time; time.sleep(60)")
    child = subprocess.Popen(command, start_new_session=False)
    neighbor = subprocess.Popen(command, start_new_session=False)
    try:
        captured = ProcessIdentity.capture(child.pid)
        assert isinstance(captured, GroupMember)
        stored = FieldCodec.decode(ProcessIdentity, FieldCodec.encode(captured))
        assert captured == stored and hash(captured) == hash(stored)
        await retire(owner(child, stored))
        child.wait(timeout=5)
        assert not captured.alive()
        assert neighbor.poll() is None
        assert ProcessIdentity.capture(os.getpid()).alive()
    finally:
        for process in (child, neighbor):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_stored_leader_keeps_group_scope_after_term_exits_leader(tmp_path: Path):
    acknowledgement = tmp_path / "stored-tree.json"
    child = ParentedProcess.launch((sys.executable, "-c", TREE, str(acknowledgement)))
    stored = FieldCodec.decode(ProcessIdentity, FieldCodec.encode(child.identity))
    try:
        await ready(acknowledgement)
        await ObservedProcess(stored).stop()
        assert all(not member.alive() for member in identities(acknowledgement))
    finally:
        await child.stop()


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
    acknowledgement = tmp_path / "tree.json"
    task = asyncio.create_task(
        BoundedRun.run((sys.executable, "-c", TREE, str(acknowledgement)), timeout=10)
    )
    await ready(acknowledgement)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(not member.alive() for member in identities(acknowledgement))


def test_new_outcome_is_derived_and_field_encoded() -> None:
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class TestObservationOutcome(ChildOutcome):
        observation: str

        @property
        def summary(self) -> str:
            return self.observation

        @property
        def successful(self) -> bool:
            return False

    try:
        value = TestObservationOutcome("observed")
        assert ChildOutcome.member_named("test_observation") is TestObservationOutcome
        assert FieldCodec.decode(ChildOutcome, FieldCodec.encode(value)) == value
    finally:
        ChildOutcome.__registry__.pop("test_observation")


@pytest.mark.asyncio
async def test_namespace_deadline_kills_group_escaping_descendant(tmp_path: Path) -> None:
    from agent_comms.child_process import NamespaceContainment, NamespacedChild, SignaledOutcome

    if not isinstance(Platform.current(), NamespaceContainment):
        pytest.skip("This platform does not provide PID namespaces")
    acknowledgement = tmp_path / "escaped"
    command = (
        sys.executable,
        "-c",
        """import subprocess,sys,time
code=('import os,sys,time; os.setsid(); '
      'open(sys.argv[1],"w").write("alive"); time.sleep(60)')
subprocess.Popen([sys.executable,'-c',code,sys.argv[1]])
time.sleep(60)
""",
        str(acknowledgement),
    )
    child = await NamespacedChild.start(command, deadline=time.monotonic() + 2)
    try:
        await ready(acknowledgement)
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
    acknowledgement = tmp_path / "namespace.json"
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
            str(acknowledgement),
        )
    )
    try:
        await ready(acknowledgement)
        namespace = json.loads(acknowledgement.read_text())
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
    acknowledgement = tmp_path / "ready"
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
                str(acknowledgement),
            ),
            env=life.environment,
            pass_fds=(life.read_fd,),
        )
        await ready(acknowledgement)
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
    acknowledgement = tmp_path / "started"
    recorded = []

    def reserve(identity: ProcessIdentity) -> None:
        assert not acknowledgement.exists()
        assert identity.alive()
        recorded.append(identity)

    child = ParentedProcess.launch(
        (
            sys.executable,
            "-c",
            'import sys,time; open(sys.argv[1],"w").write("started"); time.sleep(60)',
            str(acknowledgement),
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
    acknowledgement = tmp_path / "should-not-exist"
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
                str(acknowledgement),
            ),
            before_start=reject,
        )
    assert len(captured) == 1
    assert not captured[0].alive()
    assert not acknowledgement.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["run", "session", "exchange"])
async def test_repeated_cancellation_joins_real_tree_before_return(
    tmp_path: Path, shape: str
) -> None:
    acknowledgement = tmp_path / "tree.json"

    async def operation():
        command = (sys.executable, "-c", TREE, str(acknowledgement))
        if shape == "run":
            await BoundedRun.run(command, timeout=20)
        else:
            async with getattr(BoundedRun, shape)(command, timeout=20):
                await asyncio.Event().wait()

    task = asyncio.create_task(operation())
    await ready(acknowledgement)
    task.cancel()
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(not member.alive() for member in identities(acknowledgement))


@pytest.mark.asyncio
async def test_discard_stderr_drains_past_transport_capacity_to_real_eof():
    command = (
        sys.executable,
        "-c",
        "import os; payload=b'x'*65536; "
        "[os.write(2,payload) for _ in range(128)]; print('drained',flush=True)",
    )
    async with BoundedRun.session(command, timeout=10) as child:
        drain = asyncio.create_task(child.stderr_tail())
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
async def test_parent_process_reaps_but_observation_cannot_claim_exit_code():
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


def test_parent_pipe_cleanup_closes_after_real_exception():
    with (
        pytest.raises(ValueError, match="caller failed"),
        Platform.current().launch(
            (sys.executable, "-c", "import time; time.sleep(60)"), (), LaunchRuntime.running()
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
