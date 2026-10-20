"""ACP client ingress locking: route and maintenance locks for one child.

A client (Toad) never takes these locks itself. It awaits this owner, which
acquires every lock in a worker thread so the client's event loop never waits
on a file lock, and then performs its guarded write on its own loop because
asyncio streams are loop-owned. Release re-enters the acquisition's Context
before closing its descriptors.

Lock order is fixed: the route lock precedes the maintenance/wire gates, and
the gates of the selected and attached roots are taken in registry-path order,
so attachments of different clients cannot deadlock across roots.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
from collections.abc import AsyncIterator, Mapping
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from .child_process import AttachedChild, ChildStdio, StreamingChildStdio
from .launch_root import LaunchRoot
from .locked_store import RegistryFile
from .maintenance_barrier import MaintenanceBarrier
from .route_selection import RouteChanged, RouteSelection, child_root


def _release_abandoned(task: asyncio.Future[ExitStack]) -> None:
    if not task.cancelled() and task.exception() is None:
        task.result().close()


@dataclass(frozen=True)
class AcpIngress:
    """One ACP child's ingress: its selected route plus an attached wire root.

    ``attached`` is a coordination root reported by the child. It cannot
    redirect locking away from the selected root; both are locked.
    """

    selection: RouteSelection
    cwd: Path
    attached: str | None = None

    def _registries(self) -> list[Path]:
        roots = {self.selection.root}
        if self.attached is not None:
            attached = Path(self.attached).expanduser()
            roots.add(attached if attached.is_absolute() else self.cwd / attached)
        return sorted({RegistryFile.path_in(root.resolve()) for root in roots})

    def _lock_roots(self, locks: ExitStack) -> None:
        """Hold shared root locks; maintenance takes them exclusively."""
        for registry in self._registries():
            locks.enter_context(MaintenanceBarrier(registry).allow_input())

    def _lock_prompt(self, locks: ExitStack) -> None:
        locks.enter_context(self.selection.route.hold_client_route())
        self._lock_roots(locks)

    @staticmethod
    async def _acquire(lock_roots) -> ExitStack:
        """Acquire and release in one Context, including an abandoned waiter."""
        context = contextvars.copy_context()

        def acquire() -> ExitStack:
            with ExitStack() as locks:
                lock_roots(locks)
                release = ExitStack()
                release.callback(context.run, locks.pop_all().close)
                return release

        task = asyncio.ensure_future(asyncio.to_thread(context.run, acquire))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_release_abandoned)
            raise

    async def preflight(self) -> None:
        """Early denial only: the later spawn or prompt holds the actual gate."""
        (await self._acquire(self._lock_roots)).close()

    @asynccontextmanager
    async def prompt(self) -> AsyncIterator[None]:
        """Hold route and wire locks through one synchronous stdin write.

        Do not await inside: release before pipe drain or any native response.
        """
        with await self._acquire(self._lock_prompt):
            yield

    async def spawn(
        self, argv: tuple[str, ...], *, env: Mapping[str, str],
        accepts_launch_root: bool = True,
        pass_fds: tuple[int, ...] = (), stdio: ChildStdio = StreamingChildStdio(),
    ) -> AttachedChild:
        """Hold route and wire locks until the asynchronous spawn settles.

        A worker owns the synchronous locking while the subprocess starts on
        the caller's loop. A cancelled caller still waits for the exact spawn
        and retires its child; the gate is never dropped while an unobserved
        spawn can complete later. A permanently stuck OS spawn can prevent a
        maintenance pause from completing; there is no bounded deadline.
        """
        loop = asyncio.get_running_loop()
        child_env = LaunchRoot.environment(env)
        cwd = str(self.cwd)
        process_ready: concurrent.futures.Future[AttachedChild] = concurrent.futures.Future()
        decision: concurrent.futures.Future[bool] = concurrent.futures.Future()

        async def settle(task: asyncio.Future) -> tuple[object, bool]:
            """Repeated caller cancellation cannot abandon a lock-held spawn."""
            cancelled = False
            while not task.done():
                try:
                    await asyncio.wait((task,))
                except asyncio.CancelledError:
                    cancelled = True
            return task.result(), cancelled

        def spawn_under_lock() -> None:
            try:
                if child_root(child_env, cwd) != self.selection.root:
                    raise ValueError("ACP child environment conflicts with its selected route")
                # Route SH precedes the wire gate, matching the publisher's
                # route EX -> private-root preflight order.
                with self.selection.route.hold_client_route(), ExitStack() as locks:
                    self._lock_roots(locks)
                    launch = LaunchRoot.current()
                    if launch.path.resolve() != self.selection.root:
                        raise RouteChanged("ACP child launch root differs from selected route")
                    process = asyncio.run_coroutine_threadsafe(
                        AttachedChild.start(
                            (*argv, *(launch.arguments() if accepts_launch_root else ())),
                            env=child_env, cwd=cwd, pass_fds=pass_fds, stdio=stdio,
                        ), loop,
                    ).result()
                    process_ready.set_result(process)
                    # The caller accepts the observed process or asks for its
                    # retirement before the gate is released to maintenance.
                    if not decision.result():
                        asyncio.run_coroutine_threadsafe(process.stop(), loop).result()
            except BaseException as error:
                if not process_ready.done():
                    process_ready.set_exception(error)
                else:
                    raise

        worker = asyncio.ensure_future(asyncio.to_thread(spawn_under_lock))
        try:
            process, cancelled = await settle(asyncio.wrap_future(process_ready))
        except BaseException:
            await settle(worker)
            raise
        if cancelled:
            decision.set_result(False)
            await settle(worker)
            raise asyncio.CancelledError
        decision.set_result(True)
        # No await between acceptance and returning ownership to the caller.
        return process
