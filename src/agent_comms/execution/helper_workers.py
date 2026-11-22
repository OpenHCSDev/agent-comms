"""The worker processes an application lends its CPU work to, so that work never takes the
interpreter lock from the thread that draws.

Two members. ``SpawnedWorkers`` is CPython's process pool: fresh interpreters that import
what they run. ``ForkedWorkers`` is for a Python built without named semaphores (an Android
app's), where that pool cannot be built at all: its workers are forks of the application, so
they hold the application's loaded modules from birth and import nothing.

A process with many threads must not be forked at an arbitrary moment: the child keeps only
the forking thread, and a lock another thread held stays held in it for ever. So the
application is forked once, by ``ForkedTemplate``, into a template that has one thread and
does nothing but fork again. Every worker, the first ones and each replacement, is a fork of
that quiet template.
"""

from __future__ import annotations

import gc
import os
import pickle
import signal
import socket
import threading
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import suppress
from functools import partial
from struct import Struct
from typing import ClassVar, NoReturn

FRAME = Struct("!Q")
PID = Struct("!q")


class HelperWorkers(ABC):
    """A bounded set of processes that run submitted calls: picklable callables, arguments
    and results. One call occupies one worker until it returns."""

    @abstractmethod
    def start(self) -> None:
        """Have every worker exist now, instead of at the first call that needs one."""

    @abstractmethod
    def submit[T, **P](self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        """Run the call in a worker; its result or its exception arrives on the future."""

    @abstractmethod
    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        """Accept no more calls; with ``wait``, return once accepted calls have finished and
        the workers have left."""

    @abstractmethod
    def terminate_workers(self) -> None:
        """Stop the workers now; a call still running in one is lost."""


class SpawnedWorkers(ProcessPoolExecutor, HelperWorkers):
    """CPython's process pool over a multiprocessing context."""

    def start(self) -> None:
        """Start the configured workers without occupying one with a dummy call.

        ProcessPoolExecutor has no public prestart operation. Its own launch hook starts
        the workers before the manager thread; submission, worker replacement and shutdown
        remain executor-owned.
        """
        try:
            self._launch_processes()
            self._start_executor_manager_thread()
        except BaseException:
            self.shutdown(wait=True, cancel_futures=True)
            raise


class LostHelperWorkerError(ConnectionError):
    """A forked worker's socket closed under a call: the worker died or was killed."""


def _read_exact(stream: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.recv(size - len(chunks))
        if not chunk:
            raise LostHelperWorkerError("The other end of a helper's socket closed")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_frame(stream: socket.socket) -> bytes:
    size, = FRAME.unpack(_read_exact(stream, FRAME.size))
    return _read_exact(stream, size)


def _write_frame(stream: socket.socket, payload: bytes) -> None:
    stream.sendall(FRAME.pack(len(payload)) + payload)


def _leave(serve: Callable[[], None]) -> NoReturn:
    """A forked process's whole life is ``serve``; it then leaves without unwinding. The
    frames above this one, the exit hooks and the objects' finalizers are all the
    application's, and none of them may run in a copy of it."""
    status = 1
    try:
        serve()
        status = 0
    except BaseException:  # noqa: BLE001 -- nothing may propagate into the application's frames
        traceback.print_exc()
    finally:
        os._exit(status)


def _serve_calls(stream: socket.socket) -> None:
    """A worker: one call in, its result or exception out, until the application closes the
    socket or is gone."""
    while True:
        try:
            request = _read_frame(stream)
        except OSError:
            return
        try:
            function, args, kwargs = pickle.loads(request)
            answer = pickle.dumps((True, function(*args, **kwargs)), protocol=5)
        except Exception as failure:  # noqa: BLE001 -- the failure crosses to the caller
            failure.add_note(f"In helper worker {os.getpid()}:\n{traceback.format_exc()}")
            try:
                answer = pickle.dumps((False, failure), protocol=5)
            except Exception:  # noqa: BLE001 -- an exception that cannot cross is named instead
                answer = pickle.dumps((False, RuntimeError(f"{type(failure).__name__}: {failure}")))
        try:
            _write_frame(stream, answer)
        except OSError:
            return


class ForkedWorker:
    """One worker's socket, and the pid the template reported for it."""

    def __init__(self, pid: int, stream: socket.socket) -> None:
        self.pid = pid
        self.stream = stream

    def alive(self) -> bool:
        """A dead worker's socket reads as closed; a live idle one has nothing to read."""
        try:
            return self.stream.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) != b""
        except BlockingIOError:
            return True
        except OSError:
            return False

    def call[T, **P](self, function: Callable[P, T], args: P.args, kwargs: P.kwargs) -> T:
        """Run on a dispatch thread, which waits in the socket with the interpreter lock
        released."""
        request = pickle.dumps((function, args, kwargs), protocol=5)
        try:
            _write_frame(self.stream, request)
            succeeded, value = pickle.loads(_read_frame(self.stream))
        except LostHelperWorkerError:
            raise
        except OSError as failure:
            raise LostHelperWorkerError(str(failure)) from failure
        if not succeeded:
            raise value
        return value

    def close(self) -> None:
        """The closed socket is an idle worker's signal to leave; this returns once it has."""
        with suppress(OSError):
            self.stream.shutdown(socket.SHUT_WR)
            while self.stream.recv(4096):
                pass  # its end closes when it leaves
        self.stream.close()
        self._left()

    def kill(self) -> None:
        with suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGKILL)
        self.stream.close()
        self._left()

    def _left(self) -> None:
        """The worker is the template's child, which the kernel reaps for it: its pid stops
        existing a moment after its socket closes."""
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.0005)


class ForkedTemplate:
    """The application forked once, into a process with one thread that forks each worker.

    The fork is taken where the application chooses (``ForkedWorkers.start`` on its main
    thread, once its modules are loaded): the template is that moment's memory, so a worker
    holds every module loaded by then and imports only what was loaded later. The template
    keeps one socket to the application and nothing else of it: not its connections, not
    its event loop, not its windows. It never runs application code; a worker runs only the
    calls it is sent.
    """

    _current: ClassVar[ForkedTemplate | None] = None
    _creation: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        ours, theirs = socket.socketpair()
        # The application's objects become permanent before the fork: a collection in a
        # fork then never examines them, so no copy of an application object (a window's
        # wrapper, a connection) is ever finalized there; and they stay shared pages. The
        # application keeps them permanent, as its own start already made its imports.
        gc.freeze()
        pid = os.fork()
        if pid == 0:
            _leave(partial(self._serve_forks, theirs))
        theirs.close()
        self.pid = pid
        self.control = ours
        self.requests = threading.Lock()

    @classmethod
    def current(cls) -> ForkedTemplate:
        with cls._creation:
            if cls._current is None or not cls._current.alive():
                if cls._current is not None:
                    cls._current.close()
                cls._current = cls()
            return cls._current

    def alive(self) -> bool:
        try:
            return self.control.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) != b""
        except BlockingIOError:
            return True
        except OSError:
            return False

    def close(self) -> None:
        self.control.close()
        with suppress(ChildProcessError):
            os.waitpid(self.pid, 0)

    @staticmethod
    def _keep_only(stream: socket.socket) -> None:
        """Every other descriptor is the application's; a copy held here would keep each
        of its connections open after it closed them."""
        signal.set_wakeup_fd(-1)
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(number, signal.SIG_DFL)
        keep = stream.fileno()
        os.closerange(3, keep)
        os.closerange(keep + 1, min(os.sysconf("SC_OPEN_MAX"), 1 << 16))

    @classmethod
    def _serve_forks(cls, control: socket.socket) -> None:
        """The template: each request names a worker's initializer; the answer is the
        worker's pid and the application's end of its socket."""
        cls._keep_only(control)
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # the kernel reaps workers that leave
        while True:
            try:
                initializer, initargs = pickle.loads(_read_frame(control))
            except OSError:
                return
            application_end, worker_end = socket.socketpair()
            gc.freeze()
            pid = os.fork()
            if pid == 0:
                control.close()
                application_end.close()
                _leave(partial(cls._work, worker_end, initializer, initargs))
            worker_end.close()
            try:
                socket.send_fds(control, [PID.pack(pid)], [application_end.fileno()])
            except OSError:
                return
            finally:
                application_end.close()

    @staticmethod
    def _work(stream: socket.socket, initializer: Callable[..., object] | None,
              initargs: tuple[object, ...]) -> None:
        if initializer is not None:
            initializer(*initargs)
        _serve_calls(stream)

    def worker(self, initializer: Callable[..., object] | None,
               initargs: tuple[object, ...]) -> ForkedWorker:
        with self.requests:
            try:
                _write_frame(self.control, pickle.dumps((initializer, initargs), protocol=5))
                packed, descriptors, _flags, _address = socket.recv_fds(self.control, PID.size, 1)
            except OSError as failure:
                raise LostHelperWorkerError(f"The fork template is gone: {failure}") from failure
            if len(packed) != PID.size or not descriptors:
                raise LostHelperWorkerError("The fork template is gone")
        pid, = PID.unpack(packed)
        return ForkedWorker(pid, socket.socket(fileno=descriptors[0]))


class ForkedWorkers(HelperWorkers):
    """Workers forked from the application's template. A worker found dead before a call is
    replaced; one that dies under a call fails that call (``LostHelperWorkerError``) and is
    replaced for the next."""

    def __init__(self, max_workers: int, initializer: Callable[..., object] | None = None,
                 initargs: tuple[object, ...] = ()) -> None:
        if max_workers < 1:
            raise ValueError("Workers need at least one process")
        self._count = max_workers
        self._initializer = initializer
        self._initargs = initargs
        self._lock = threading.Lock()
        self._idle: list[ForkedWorker] = []
        self._busy: set[ForkedWorker] = set()
        self._closed = False
        # One thread per worker carries a call to it and waits; a call beyond the workers'
        # number waits its turn here, cancellable until a thread takes it.
        self._dispatch = ThreadPoolExecutor(max_workers, thread_name_prefix="helper-call")

    def _fill(self) -> None:
        while len(self._idle) + len(self._busy) < self._count:
            self._idle.append(ForkedTemplate.current().worker(self._initializer, self._initargs))

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("These helper workers are shut down")
            self._fill()

    def _take(self) -> ForkedWorker:
        with self._lock:
            if self._closed:
                raise RuntimeError("These helper workers are shut down")
            for worker in tuple(self._idle):
                if not worker.alive():
                    self._idle.remove(worker)
                    worker.close()
            self._fill()
            worker = self._idle.pop()
            self._busy.add(worker)
            return worker

    def _call[T, **P](self, function: Callable[P, T], args: P.args, kwargs: P.kwargs) -> T:
        worker = self._take()
        lost = False
        try:
            return worker.call(function, args, kwargs)
        except LostHelperWorkerError:
            lost = True
            raise
        finally:
            with self._lock:
                self._busy.discard(worker)
                if lost or self._closed:
                    worker.close()
                else:
                    self._idle.append(worker)

    def submit[T, **P](self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        return self._dispatch.submit(self._call, fn, args, kwargs)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for worker in idle:
            worker.close()
        # A worker under a call is closed by its dispatch thread when the call returns.
        self._dispatch.shutdown(wait=wait, cancel_futures=cancel_futures)

    def terminate_workers(self) -> None:
        with self._lock:
            self._closed = True
            workers, self._idle = [*self._idle, *self._busy], []
        for worker in workers:
            worker.kill()
