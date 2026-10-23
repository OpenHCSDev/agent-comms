"""The one git identity every test repository commits under, given to each command.

No test writes identity or signing into a repository's config. When GIT_DIR leaks in
(``git bisect run`` from a linked worktree, a hook), ``git config`` writes the caller's
repository instead of the fixture's: on 10-23 two fixtures stamped "Citation fixture" and
"Ratchet test" into the shared agent-comms config, and 106 real commits carried the second.
A setting given per command persists nothing, whichever repository git resolves."""

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
