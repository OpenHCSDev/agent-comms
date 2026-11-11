"""The worker host process: `-m` runs `__main__`, the host program.

This package holds only what the host sets on itself, so a launcher can import it and
name the worker without importing the bus the worker serves."""
import os
import resource

# Native children inherit the host's background priority, not an interactive UI's.
OWNER_NICENESS = 10


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
