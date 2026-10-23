"""Real OS behavior for the shared child owner; no simulated subprocesses."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from contextlib import closing, contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.child_process import (
    AttachedChild,
    BoundedRun,
    ChildOutcome,
    DiesWithLauncherLifetime,
    ExitedOutcome,
    ForcedStopOutcome,
    GroupMember,
    IdentityMismatchError,
    ObservedProcess,
    Platform,
    ProcessIdentity,
    STOP_GRACE_SECONDS,
    SpawnedProcess,
    SynchronousProcess,
    TimedOutOutcome,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.launch_runtime import LaunchRuntime
from process_tree_fixture import TREE, identities, ready


def test_wait_any_exit_returns_before_another_member_exits() -> None:
    first = SpawnedProcess.launch((sys.executable, "-c", "import time; time.sleep(.1)"))
    last = SpawnedProcess.launch((sys.executable, "-c", "import time; time.sleep(60)"))
    try:
        started = time.monotonic()
        Platform.current().wait_any_exited((first.identity, last.identity), 10)
        assert time.monotonic() - started < 5
        assert not first.alive()
        assert last.alive()
    finally:
        first.close()
        last.close()


def test_wait_any_exit_returns_for_reused_identity_and_exited_member() -> None:
    child = SpawnedProcess.launch((sys.executable, "-c", "import time; time.sleep(60)"))
    platform = Platform.current()
    try:
        # Another incarnation at the pid means the awaited member already exited.
        started = time.monotonic()
        platform.wait_any_exited(
            (replace(child.identity, start_time=child.identity.start_time + 1),), 10,
        )
        assert time.monotonic() - started < 5
        assert child.alive()
    finally:
        child.close()
    started = time.monotonic()
    platform.wait_any_exited((child.identity,), 10)
    assert time.monotonic() - started < 5


def test_stop_batch_shares_one_force_deadline() -> None:
    command = (sys.executable, "-c", (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready',flush=True); time.sleep(60)"
    ))
    children = tuple(SpawnedProcess.launch(command, output=subprocess.PIPE) for _ in range(2))
    try:
        for child in children:
            assert child.process.stdout is not None
            assert child.process.stdout.readline() == b"ready\n"
        signalled: dict[int, list[float]] = {index: [] for index in range(len(children))}

        def recording(index: int):
            @contextmanager
            def guard():
                signalled[index].append(time.monotonic())
                yield
            return guard

        outcomes = SynchronousProcess.stop_together(
            tuple((child, recording(index)) for index, child in enumerate(children))
        )
        assert all(isinstance(outcome, ForcedStopOutcome) for outcome in outcomes)
        # Each plan's first guarded signal is the graceful pass, its second the force.
        # One shared deadline forces both within one grace period, whatever the load.
        forced = [times[1] for times in signalled.values()]
        assert max(forced) - min(forced) < STOP_GRACE_SECONDS
        assert all(child.retired for child in children)
    finally:
        for child in children:
            child.close()


@pytest.mark.asyncio
async def test_bounded_run_retires_real_child_and_defiant_grandchild(tmp_path: Path) -> None:
    acknowledgement = tmp_path / "tree.json"
    async with BoundedRun.session((sys.executable, "-c", TREE, str(acknowledgement)), timeout=30) as child:
        # The child announces both live identities; machine load is not readiness.
        assert await child.stdout.readline() == b"tree ready\n"
        outcome = await child.finish()
        assert isinstance(outcome, TimedOutOutcome)
        assert all(not member.alive() for member in identities(acknowledgement))


@pytest.mark.skipif(os.name != "posix", reason="The ratchet uses POSIX fork workers")
@pytest.mark.asyncio
async def test_fork_pool_parent_death_retires_workers_and_nested_child_group(tmp_path: Path) -> None:
    announcement = tmp_path / "pool-tree.json"
    child = SpawnedProcess.launch(
        (sys.executable, str(Path(__file__).with_name("process_pool_child.py")), str(announcement)),
        output=subprocess.PIPE,
    )
    observed = ()
    try:
        await ready(announcement)
        observed = identities(announcement)
        assert all(member.alive() for member in observed)
        assert len(child.platform.group_members(child.identity)) > 1
        # Kill only the parent: no context exit and no outer group signal. The
        # worker's own lifeline and current child owner must do the retirement.
        child.process.kill()
        child.process.wait(timeout=5)
        async with asyncio.timeout(8):
            while child.platform.group_members(child.identity) or any(p.alive() for p in observed):
                await asyncio.sleep(.01)
        assert all(not member.alive() for member in observed)
    finally:
        child.close()
        for member in observed:
            if member.alive():
                ObservedProcess(member).stop_sync()


def test_synchronous_run_collects_both_streams_and_returncode(tmp_path: Path) -> None:
    result = SpawnedProcess.run(
        (sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"),
        timeout=5, cwd=tmp_path, env=dict(os.environ),
    )
    assert (result.returncode, result.stdout, result.stderr) == (7, b"out\n", b"err\n")


def test_child_imports_the_checkout_its_parent_imported(tmp_path: Path) -> None:
    import agent_comms

    # No inherited import path: only the owner can point the child at this checkout,
    # not the editable install's .pth, which may name another worktree.
    environment = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    result = SpawnedProcess.run(
        (sys.executable, "-c", "import agent_comms; print(agent_comms.__file__)"),
        timeout=30, cwd=tmp_path, env=environment,
    )
    assert result.returncode == 0, result.stderr
    checkout = Path(__file__).resolve().parents[1]
    assert Path(agent_comms.__file__).resolve().is_relative_to(checkout)
    assert Path(result.stdout.decode().strip()).resolve().is_relative_to(checkout)


def test_grandchild_import_path_holds_one_copy_of_the_checkout(tmp_path: Path) -> None:
    # The child relaunches through the same owner: two generations of real launches.
    report = (
        "import os,sys; from pathlib import Path; "
        "from agent_comms.child_process import SpawnedProcess; "
        "r = SpawnedProcess.run((sys.executable, '-c', "
        "'import os; print(os.environ[\"PYTHONPATH\"])'), "
        "timeout=30, cwd=Path.cwd(), env=dict(os.environ)); "
        "sys.stdout.buffer.write(r.stdout)"
    )
    environment = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    result = SpawnedProcess.run(
        (sys.executable, "-c", report), timeout=60, cwd=tmp_path, env=environment
    )
    assert result.returncode == 0, result.stderr
    import agent_comms

    root = str(Path(agent_comms.__file__).resolve().parents[1])
    assert result.stdout.decode().strip().split(os.pathsep).count(root) == 1


def test_stop_batch_retires_live_child_after_an_original_identity_exits() -> None:
    exited = SpawnedProcess.launch((sys.executable, "-c", "pass"))
    live = SpawnedProcess.launch((sys.executable, "-c", "import time; time.sleep(60)"))
    try:
        exited.process.wait(timeout=5)
        original = ObservedProcess(exited.identity)
        with pytest.raises(ProcessLookupError):
            SynchronousProcess.stop_together(((original, nullcontext), (live, nullcontext)))
        assert live.retired
    finally:
        live.close()
        exited.close()


@pytest.mark.asyncio
async def test_detached_mismatch_refuses_signal_without_harming_real_child() -> None:
    child = SpawnedProcess.launch((sys.executable, "-c", "import time; time.sleep(60)"))
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
    pytest.param(SpawnedProcess, id="spawned"),
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
    child = SpawnedProcess.launch((sys.executable, "-c", TREE, str(acknowledgement)))
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
async def test_exec_failure_is_reported_by_real_exec_boundary(tmp_path: Path) -> None:
    from agent_comms.child_process import FailedToStartOutcome

    result = await BoundedRun.run((str(tmp_path / "missing-executable"),), timeout=2)
    assert isinstance(result.outcome, FailedToStartOutcome)


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
            with pytest.raises(sqlite3.OperationalError):
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


LAUNCHER = """import json,sys,time
from pathlib import Path
from agent_comms.child_process import OutlivesLauncherLifetime, SpawnedProcess
from agent_comms.field_codec import FieldCodec
sleeper = (sys.executable, '-c', 'import time; time.sleep(60)')
children = (SpawnedProcess.launch(sleeper),
            SpawnedProcess.launch(sleeper, lifetime=OutlivesLauncherLifetime))
Path(sys.argv[1]).write_text(json.dumps([FieldCodec.encode(c.identity) for c in children]))
time.sleep(60)
"""


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_PDEATHSIG is the Linux binding")
@pytest.mark.asyncio
async def test_killed_launcher_takes_its_child_and_leaves_its_resident(tmp_path: Path) -> None:
    announcement = tmp_path / "children.json"
    launcher = SpawnedProcess.launch((sys.executable, "-c", LAUNCHER, str(announcement)))
    observed = ()
    try:
        await ready(announcement)
        tied, resident = observed = identities(announcement)
        assert tied.alive() and resident.alive()
        # SIGKILL runs no exit handler: only the kernel binding can retire the child.
        launcher.process.kill()
        launcher.process.wait(timeout=5)
        async with asyncio.timeout(2):
            while tied.alive():
                await asyncio.sleep(.01)
        assert resident.alive(), "a declared resident must outlive its launcher"
    finally:
        launcher.close()
        for member in observed:
            if member.alive():
                ObservedProcess(member).stop_sync()


def test_detached_reserves_identity_before_child_can_execute(tmp_path: Path) -> None:
    acknowledgement = tmp_path / "started"
    with SpawnedProcess.launching(
        (
            sys.executable,
            "-c",
            'import sys,time; open(sys.argv[1],"w").write("started"); time.sleep(60)',
            str(acknowledgement),
        ),
    ) as child:
        assert not acknowledgement.exists()
        assert child.identity.alive()
    try:
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

    with pytest.raises(ValueError), SpawnedProcess.launching(
        (
            sys.executable,
            "-c",
            "import sys; open(sys.argv[1], 'w').write('wrong')",
            str(acknowledgement),
        ),
    ) as child:
        captured.append(child.identity)
        raise ValueError("reservation refused")
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

    child = SpawnedProcess.launch((sys.executable, "-c", "import time; time.sleep(.1); exit(7)"))
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
        pytest.raises(ValueError),
        Platform.current().launch(
            (sys.executable, "-c", "import time; time.sleep(60)"), (), LaunchRuntime.running(),
            DiesWithLauncherLifetime,
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
