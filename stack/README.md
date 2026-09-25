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
stack/bin/prepare-pi-native
```

`stack/bin/toad-comms [THREAD]` opens the pinned Toad UI on an existing
thread. Link that script into your `PATH` if you want the short command name.
It attaches to the current owner and does not send a prompt.

`prepare-pi-native` verifies the installed Pi 0.85.1 bytes, builds a pinned
local copy with native input IDs and bounded compaction, and checks the resulting files. Each
manifest gets its own copy, so preparing an update leaves running workers on
their previous package until they restart. The copy
retains the stock Bedrock transport; the script does not change the installed
Pi or make a provider call. Set `PI_STOCK_DIR` if Pi is installed elsewhere.
The `toad-comms` launcher uses this copy so a direct prompt can produce the
required native user-start receipt. Existing Pi session directories and files
must be private (0700 directory, 0600 file) before a tracked prompt; the native
preflight refuses unsafe sessions rather than changing their permissions.

All three packages are installed from immutable Git commits. Toad also pins
agent-comms in its own manifest, so both agent-comms pins must agree. Update
their revisions in `pyproject.toml`, run `uv lock --project stack`, and commit
the manifest and lockfile together.

The coordination wire defaults to `~/.agent-comms`; set `AGENT_COMMS_ROOT` if
your state lives elsewhere. Installing this project does not launch owners or
submit prompts.

Unread reply counts keep a disposable `transcript_reply_index.sqlite3` in the
wire root. Existing transcripts are indexed once; later appends and fresh UI
processes use the index. If that file is removed or damaged, the next read
rebuilds it from the native transcripts.

## Compaction strategy

Pi settings remain the authority for `compaction.enabled`, `reserveTokens`,
`keepRecentTokens`, and the selected model's context window. The native
`CompactionPolicy` declaration in `native-compaction-policy.mjs` owns adapter
strategy, concurrency, input packing, and summary output limits. The prepared
native copy imports that module; its bytes are pinned in `pi-native.sha256`.

To select a strategy for newly launched owners, set the configuration in their
environment, for example:

```sh
export AGENT_COMMS_COMPACTION_POLICY='{"strategy":"parallel","concurrency":2}'
```

Unspecified fields use declaration defaults. Unknown fields, unsupported
strategies, or invalid values fail before a summary request. `serial` runs the
same bounded algorithm with one worker. `parallel` runs independent source
segments concurrently and synthesizes their results in chronological order.
Large intermediate summaries are reduced through additional bounded levels;
no segment is silently truncated. A provider-native strategy is not implemented.

This adapter has no authoritative local tokenizer. It conservatively bounds
serialized UTF-8 input against the selected model's token budget, including
headroom, and checks each completed prompt before sending. Parallel work
reduces serial latency; it does not claim lossless summaries or constant-time
processing of arbitrarily long active context. Previously compacted history is
represented by its saved summary and retained recent window.

No-replay is unconditional: a failed or uncertain request is never retried.
Failure aborts other in-flight map requests and prevents new maps or synthesis;
no partial compaction is committed. Provider usage is recorded per response.
Progress counts completed source bytes once, then reports a separate synthesis
phase. Stop/abort applies to all work within the compaction.
