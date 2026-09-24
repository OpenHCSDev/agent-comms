# Local agent-comms stack

This uv project installs agent-comms alongside the merged Toad and Textual
forks at fixed commits. It requires Python 3.14. The three source locations
are declared in `pyproject.toml`; `uv.lock` records the
resolved dependency set.

From the repository root:

```sh
uv sync --project stack --locked --python 3.14
uv run --project stack --locked agent-comms --help
uv run --project stack --locked toad
```

All three packages are installed from immutable Git commits. Toad also pins
agent-comms in its own manifest, so both agent-comms pins must agree. Update
their revisions in `pyproject.toml`, run `uv lock --project stack`, and commit
the manifest and lockfile together.

The coordination wire defaults to `~/.agent-comms`; set `AGENT_COMMS_ROOT` if
your state lives elsewhere. Installing this project does not launch owners or
submit prompts.
