"""The worker host process: `-m` runs `__main__`, the host program.

This package holds only what the host sets on itself, so a launcher can import it and
name the worker without importing the bus the worker serves."""
import os
import resource
import sys

# Native children inherit the host's background priority, not an interactive UI's.
OWNER_NICENESS = 10

# How long a thread running Python keeps the GIL while another thread waits for it.
GIL_SWITCH_INTERVAL = 0.0002


def lower_priority() -> None:
    current = os.getpriority(os.PRIO_PROCESS, 0)
    os.setpriority(os.PRIO_PROCESS, 0, max(current, OWNER_NICENESS))


def open_every_file_this_host_may_need() -> None:
    """Take the open-file budget this host's own kernel already allows it.

    One host answers every session on the bus, and each one holds its stores open: a
    live host sat at 748 descriptors, 157 of them on registry.sqlite3, against the 1024
    soft limit a login shell happened to hand it, while its hard limit was 524288. The
    next open() to run out of budget was an unrelated diagnostics file, so the refusal
    named that file instead of the budget, and the read that failed had nothing to do
    with the sessions holding the descriptors. The soft limit is this process's to set.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < hard:
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))


def hand_the_gil_back_quickly() -> None:
    """A thread waiting for the GIL waits until the thread holding it is asked to let go,
    and Python asks only once per switch interval, 5 ms by default.

    This host's coordinator thread runs hundreds of short SQLite statements per message, and
    every statement releases the GIL and takes it back. While the event loop or a pool
    thread runs Python (decoding Pi's events, building prompts), each take-back waited
    for that interval: two indexed statements took 0.16 ms alone and 31 ms beside one
    Python thread. At 0.2 ms they take 1.5 ms there, and the Python thread did the same
    work in the same CPU time at every interval measured (5, 1, 0.5 and 0.2 ms;
    2026-11-14)."""
    sys.setswitchinterval(GIL_SWITCH_INTERVAL)
