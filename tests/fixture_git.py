"""The one git identity every test repository commits under, given to each command.

No test writes identity or signing into a repository's config. When GIT_DIR leaks in
(``git bisect run`` from a linked worktree, a hook), ``git config`` writes the caller's
repository instead of the fixture's: on 10-23 two fixtures stamped "Citation fixture" and
"Ratchet test" into the shared agent-comms config, and 106 real commits carried the second.
A setting given per command persists nothing, whichever repository git resolves.

Importing this module also removes the caller's repository from the environment, so every
test repository's git addresses the test repository even outside pytest's conftest."""

import os


def _without_caller_repository() -> None:
    """A caller's git selects no repository for any test git call.

    ``git bisect run`` from a linked worktree exports GIT_DIR, and a fixture's
    ``git -C tmp init`` then re-initialised the caller's checkout as bare instead of
    creating its own; ``SourceRepository.create`` would also add its remote, move ``main``
    and hard-reset the caller's work tree. Removed at import, before session start's own git
    call and before xdist workers inherit the environment."""
    for name in tuple(name for name in os.environ if name.startswith("GIT_")):
        del os.environ[name]


_without_caller_repository()

SETTINGS = (
    ("user.name", "Test fixture"),
    ("user.email", "fixture@example.invalid"),
    ("commit.gpgsign", "false"),
)

# Global options for one git command: ("git", *IDENTITY, "-C", path, ...).
IDENTITY = tuple(part for key, value in SETTINGS for part in ("-c", f"{key}={value}"))

# The same settings for a git that the code under test starts, through git's own
# GIT_CONFIG_COUNT channel; set with monkeypatch so they end with the test.
ENVIRONMENT = {
    "GIT_CONFIG_COUNT": str(len(SETTINGS)),
    **{f"GIT_CONFIG_KEY_{index}": key for index, (key, _) in enumerate(SETTINGS)},
    **{f"GIT_CONFIG_VALUE_{index}": value for index, (_, value) in enumerate(SETTINGS)},
}
