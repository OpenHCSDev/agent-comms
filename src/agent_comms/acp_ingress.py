"""ACP client ingress admission: route and maintenance custody for one child.

A client (Toad) never takes these locks itself. It awaits this owner, which
acquires every lock in a worker thread so the client's event loop never waits
on a file lock, and then performs its guarded write on its own loop because
asyncio streams are loop-owned. Release only closes descriptors.

Lock order is fixed: route custody precedes the maintenance/wire gates, and
the gates of the selected and attached roots are taken in registry-path order,
so attachments of different clients cannot deadlock across roots.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from .child_process import AttachedChild, ChildStdio, StreamingChildStdio
from .maintenance_barrier import MaintenanceBarrier
from .route_selection import RouteSelection, child_root, selected_write


def _release_abandoned(task: asyncio.Future[ExitStack]) -> None:
    if not task.cancelled() and task.exception() is None:
        task.result().close()


@dataclass(frozen=True)
class AcpIngress:
    """One ACP child's ingress: its selected route plus an attached wire root.

    ``attached`` is a coordination root reported by the child. It cannot
    redirect admission away from the selected root; both are admitted.
    """

    selection: RouteSelection
    cwd: Path
    attached: str | None = None

    def _registries(self) -> list[Path]:
        roots = {self.selection.root}
        if self.attached is not None:
            attached = Path(self.attached).expanduser()
            roots.add(attached if attached.is_absolute() else self.cwd / attached)
        return sorted({root.resolve() / "registry.json" for root in roots})

    def _admit(self, custody: ExitStack) -> None:
        """Share open-root wire custody; maintenance takes it exclusively."""
        for registry in self._registries():
            custody.enter_context(MaintenanceBarrier(registry).admit_ingress())

    def _admit_prompt(self, custody: ExitStack) -> None:
        custody.enter_context(
            selected_write(self.selection.root, implicit=self.selection.implicit)
        )
        self._admit(custody)

    @staticmethod
    async def _acquire(admit) -> ExitStack:
        """Acquire custody in a worker; a cancelled waiter never leaks it."""

        def acquire() -> ExitStack:
            with ExitStack() as custody:
                admit(custody)
                return custody.pop_all()

        task = asyncio.ensure_future(asyncio.to_thread(acquire))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_release_abandoned)
            raise

    async def preflight(self) -> None:
        """Early denial only: the later spawn or prompt holds the actual gate."""
        (await self._acquire(self._admit)).close()

    @asynccontextmanager
    async def prompt(self) -> AsyncIterator[None]:
        """Hold route and wire admission through one synchronous stdin write.

        Do not await inside: release before pipe drain or any native response.
        """
        with await self._acquire(self._admit_prompt):
            yield

    def _project_launch(self, child_env: dict[str, str]) -> None:
        """Publish the captured root into the child only after locked admission."""
        from .active_route import read_active_route
        from .private_nk_entrypoint import PACKAGE_ENV, ROOT_ID_ENV, PrivateNkLaunch

        root = self.selection.root
        child_env["AGENT_COMMS_ROOT"] = str(root)
        if self.selection.implicit:
            route = read_active_route()
            if route is None:
                return
            if route != self.selection.route:
                raise ValueError("ACP root changed before private launch")
            for key, expected in (
                (ROOT_ID_ENV, route.wire_root_id),
                (PACKAGE_ENV, str(route.native_package)),
            ):
                if key in child_env and child_env[key] != expected:
                    raise ValueError("ACP private launch identity conflicts with route")
                child_env[key] = expected
        elif ROOT_ID_ENV in child_env or PACKAGE_ENV in child_env:
            # An explicit child root is independent of the default route, but
            # inherited private-owner flags must belong to that explicit root.
            launch = PrivateNkLaunch.from_environment(root, child_env)
            if launch is None:
                raise ValueError("ACP private launch selection is absent")
            launch.validate()

    async def spawn(
        self, argv: tuple[str, ...], *, env: Mapping[str, str],
        pass_fds: tuple[int, ...] = (), stdio: ChildStdio = StreamingChildStdio(),
    ) -> AttachedChild:
        """Hold route and wire admission until the asynchronous spawn settles.

        A worker owns the synchronous admission while the subprocess starts on
        the caller's loop. A cancelled caller still waits for the exact spawn
        and retires its child; the gate is never dropped while an unobserved
        spawn can complete later. A permanently stuck OS spawn can prevent a
        maintenance pause from completing; there is no bounded deadline.
        """
        loop = asyncio.get_running_loop()
        child_env = dict(env)
        cwd = str(self.cwd)
        process_ready: concurrent.futures.Future[AttachedChild] = concurrent.futures.Future()
        decision: concurrent.futures.Future[bool] = concurrent.futures.Future()

        async def settle(task: asyncio.Future) -> tuple[object, bool]:
            """Repeated caller cancellation cannot abandon a lock-held spawn."""
            cancelled = False
            while True:
                try:
                    return await asyncio.shield(task), cancelled
                except asyncio.CancelledError:
                    cancelled = True

        def spawn_under_lock() -> None:
            try:
                if child_root(child_env, cwd) != self.selection.root:
                    raise ValueError("ACP child environment conflicts with its selected route")
                # Route SH precedes the wire gate, matching the publisher's
                # route EX -> private-root preflight order.
                with self.selection.route.admit_client(), ExitStack() as admission:
                    self._admit(admission)
                    self._project_launch(child_env)
                    process = asyncio.run_coroutine_threadsafe(
                        AttachedChild.start(
                            argv, env=child_env, cwd=cwd, pass_fds=pass_fds, stdio=stdio,
                        ), loop,
                    ).result()
                    process_ready.set_result(process)
                    # The caller accepts the observed process or asks for its
                    # retirement before the gate is released to maintenance.
                    if not decision.result():
                        while True:
                            try:
                                asyncio.run_coroutine_threadsafe(process.stop(), loop).result()
                            except Exception:
                                # Uncertain retirement must not certify a safe
                                # pause; retry only this child's cleanup.
                                time.sleep(0.1)
                                continue
                            break
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
